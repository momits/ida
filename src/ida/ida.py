import abc
import itertools as it
import shutil
import tempfile
from collections import Counter, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Tuple, Dict, Any, Optional, List, Union

import joblib
import numpy as np
from joblib import Parallel, delayed
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.model_selection import KFold, StratifiedKFold, GridSearchCV
from sklearn.pipeline import Pipeline
from sklearn.utils.validation import check_is_fitted

from ida.interpret.common import Interpreter
from ida.type1.common import Type1Explainer
from ida.type2.common import Type2Explainer
from ida.common import NestedLogger


def class_counts(predicted_classes, all_classes) -> [Tuple[str, int]]:
    """
    Returns tuples of class ids and corresponding counts of observations in the training data.
    """
    counts = Counter(it.chain(predicted_classes, all_classes))
    m = 0 if all_classes is None else 1
    return sorted(((s, c - m) for s, c in counts.items()),
                  key=lambda x: (1.0 / (x[1] + 1), x[0]))


def filter_for_split(predicted_classes, min_k_folds, all_classes):
    """
    Filters the training data so that all predicted classes appear at least
    `ceil(security_factor * num_splits)` times.
    Use this to keep only classes where at least one prediction for each CV split is available.
    """
    enough_samples = list(s for s, c in class_counts(predicted_classes, all_classes) if c >= min_k_folds)
    return np.isin(predicted_classes, enough_samples)


def determine_k_folds(predicted_classes: [int], max_k_folds: int):
    return min(max_k_folds, Counter(predicted_classes).most_common()[-1][1])


class ImageList(Sequence):

    def __init__(self, image_ids: np.ndarray, images: np.ndarray):
        assert len(image_ids.shape) == 1
        assert image_ids.shape[0] == images.shape[0]
        assert len(images.shape) == 4

        self.image_ids = image_ids
        self.images = images

    def __getitem__(self, idx):
        if isinstance(idx, Iterable):
            return ImageList(self.image_ids[idx], self.images[idx])
        elif np.issubdtype(type(idx), np.integer):
            return self.image_ids[idx], self.images[idx]
        else:
            raise ValueError(f'Cannot use {idx} of type {type(idx)} for indexing.')

    def __len__(self):
        return len(self.image_ids)


@contextmanager
def memory_mapped_image_list(image_iter: Iterable[Tuple[str, np.ndarray]]):
    image_ids, images = list(zip(*image_iter))
    image_ids = np.array(image_ids, dtype=str)
    images = np.array(images, dtype=np.uint8)

    tmp_dir = Path(tempfile.mkdtemp())
    ids_tmp_file = tmp_dir / 'ipa_image_ids.mmap'
    images_tmp_file = tmp_dir / 'ipa_images.mmap'
    for tmp_file in (ids_tmp_file, images_tmp_file):
        if tmp_file.exists():
            tmp_file.unlink()
    joblib.dump(image_ids, ids_tmp_file)
    joblib.dump(images, images_tmp_file)
    image_ids = joblib.load(ids_tmp_file, 'r')
    images = joblib.load(images_tmp_file, 'r')

    try:
        yield ImageList(image_ids, images)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_cv(images: ImageList,
           predicted_classes: List[int],
           all_classes: Optional[List[int]],
           pipeline: Pipeline,
           param_grid: Dict[str, Any],
           min_k_folds: int = 5,
           max_k_folds: int = 5,
           n_jobs: int = 62,
           pre_dispatch: Union[int, str] = 'n_jobs',
           scoring: str = 'roc_auc_ovo') -> GridSearchCV:
    predicted_classes = np.asarray(predicted_classes)

    assert min_k_folds <= max_k_folds
    indices = filter_for_split(predicted_classes=predicted_classes,
                               min_k_folds=min_k_folds,
                               all_classes=all_classes)
    if not np.any(indices):
        indices = np.ones_like(indices, dtype=bool)
        cv = KFold(n_splits=min_k_folds)
    else:
        k_folds = determine_k_folds(predicted_classes[indices], max_k_folds)
        cv = StratifiedKFold(n_splits=k_folds)

    search = GridSearchCV(estimator=pipeline,
                          param_grid=param_grid,
                          cv=cv,
                          n_jobs=n_jobs,
                          pre_dispatch=pre_dispatch,
                          scoring=scoring)

    return search.fit(images[indices], predicted_classes[indices])


class InterpretPickTransformer(abc.ABC, BaseEstimator, TransformerMixin, NestedLogger):

    def __init__(self,
                 type2: Type2Explainer,
                 threshold: float = .5,
                 quantile: bool = False,
                 n_jobs: Optional[int] = None):
        self.type2 = type2
        self.threshold = threshold
        self.quantile = quantile
        self.n_jobs = n_jobs

    @property
    def interpreter(self) -> Interpreter:
        return self.type2.interpreter

    def _get_single_counts(self, image_id: str, image: np.ndarray) -> Iterable[Tuple[List[int], List[int]]]:
        concept_ids, _, influences = list(zip(*self.type2(image=image, image_id=image_id)))
        counts = self.interpreter.concept_ids_to_counts(concept_ids)
        influential_concept_ids = (c for c, i in zip(concept_ids, influences) if i)
        influential_counts = self.interpreter.concept_ids_to_counts(influential_concept_ids)
        return counts, influential_counts

    def _get_counts(self, X):
        zipped = Parallel(n_jobs=self.n_jobs)(delayed(self._get_single_counts)(image_id, image)
                                              for image_id, image in X)
        return list(zip(*zipped))

    def _evaluate_counters(self, counts: [[int]], influential_counts: [[int]]):
        counts = np.sum(np.asarray(counts), axis=0)
        influential_counts = np.sum(np.asarray(influential_counts), axis=0)

        self.num_influential_concept_instances_ = np.sum(influential_counts)
        self.stats_ = self.type2.stats

        concept_influences = []
        for concept_id in range(len(self.interpreter.concepts)):
            influential_count = float(influential_counts[concept_id])
            total_count = float(counts[concept_id])
            if total_count == 0.:
                concept_influences.append(0.)
            else:
                concept_influences.append(influential_count / total_count)

        self.concept_influences_ = concept_influences
        inf_by_name = sorted(zip(self.interpreter.concepts, self.concept_influences_),
                             key=lambda x: x[1],
                             reverse=True)
        self.log_item(f'Final concept influences: {inf_by_name}')
        self.picked_concepts_ = self.get_influential_concepts()
        self.picked_concept_names_ = (np.asarray(self.interpreter.concepts)[self.picked_concepts_]).tolist()
        self.log_item(f'Picked concepts: {self.picked_concept_names_}')

    def fit(self, X, y=None):
        with self.log_task('Observing concept influences...'):
            counts, influential_counts = self._get_counts(X)
            self._evaluate_counters(counts, influential_counts)
            return self

    def transform(self, X):
        check_is_fitted(self)
        with self.log_task('Interpreting inputs...'):
            counts, influential_counts = self._get_counts(X)
            return np.asarray(counts)[:, self.picked_concepts_]

    def fit_transform(self, X, y=None, **fit_params):
        with self.log_task('Observing concept influences...'):
            counts, influential_counts = self._get_counts(X)
            self._evaluate_counters(counts, influential_counts)
        return np.asarray(counts)[:, self.picked_concepts_]

    def get_influential_concepts(self) -> [int]:
        if self.quantile:
            if len(np.unique(self.concept_influences_)) == 1:
                # treat all concepts as influential
                return list(range(len(self.concept_influences_)))

            threshold = np.quantile(self.concept_influences_, self.threshold)
        else:
            threshold = self.threshold
        return [idx for idx, i in enumerate(self.concept_influences_) if i > threshold]


def ipa(image_iter: Iterable[Tuple[str, np.ndarray]],
        type2: Type2Explainer,
        type1: Type1Explainer,
        model_agnostic_picker: Any,
        param_grid: Dict[str, Any],
        cv_params: Dict[str, Any],
        random_state: int,
        observe_classifier_n_jobs: Optional[int] = None,
        log_nesting: int = 0) -> GridSearchCV:

    logger = NestedLogger()
    logger.log_nesting = log_nesting

    with memory_mapped_image_list(image_iter) as images:
        with logger.log_task('Observing classifier...'):
            predicted_classes = Parallel(n_jobs=observe_classifier_n_jobs)(
                delayed(type2.classifier.predict_single)(image=image,
                                                         image_id=image_id)
                for image_id, image in images
            )

        with logger.log_task('Running IPA...'):
            pipeline = Pipeline([
                ('interpret-pick', InterpretPickTransformer(type2)),
                ('pick_agnostic', model_agnostic_picker),
                ('approximate', type1.create_pipeline(random_state=random_state))
            ])
            cv = run_cv(images=images,
                        predicted_classes=predicted_classes,
                        all_classes=list(range(type2.classifier.num_classes)),
                        pipeline=pipeline,
                        param_grid=param_grid,
                        **cv_params)
    return cv

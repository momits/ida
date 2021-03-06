import ast
import csv
import itertools as it
import sys
from collections import Counter
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import timeit
from datetime import datetime
from typing import Mapping, Any, Dict, Tuple, Iterable, List, Union

import numpy as np
from importlib import resources

import pandas as pd
import re
from petastorm import make_reader
from sklearn.feature_selection import SelectorMixin
from sklearn.metrics import log_loss, roc_auc_score

from ida.ida import ipa
from ida.interpret.common import Interpreter
from ida.torch_extensions.classifier import TorchImageClassifier
from ida.type1.common import Type1Explainer, top_k_accuracy_score, counterfactual_top_k_accuracy_metrics
from ida.common import NestedLogger, memory
from ida.type2.common import Type2Explainer


# See https://stackoverflow.com/questions/15063936
max_field_size = sys.maxsize
while True:
    try:
        csv.field_size_limit(max_field_size)
        break
    except OverflowError:
        maxInt = int(max_field_size / 10)


@contextmanager
def _prepare_image_iter(images_url, skip: int = 0) -> Iterable[Tuple[str, np.ndarray]]:
    reader = make_reader(images_url,
                         workers_count=1,  # only 1 worker to ensure determinism of results
                         shuffle_row_groups=False)
    try:
        def image_iter() -> Iterable[Tuple[str, np.ndarray]]:
            for row in it.islice(reader, skip, None):
                yield row.image_id, row.image

        yield image_iter()
    finally:
        reader.stop()
        reader.join()


@memory.cache
def _prepare_test_observations(test_images_url: str,
                               interpreter: Interpreter,
                               classifier: TorchImageClassifier,
                               num_test_obs: int) -> Tuple[List[List[int]], List[int],
                                                           List[List[int]], List[int]]:
    concept_counts = []
    predicted_classes = []
    iv_concept_counts = []
    iv_predicted_classes = []
    with _prepare_image_iter(test_images_url) as test_iter:
        for image_id, image in it.islice(test_iter, num_test_obs):
            concept_counts.append(interpreter.count_concepts(image=image, image_id=image_id))
            predicted_classes.append(classifier.predict_single(image=image))

            with suppress(StopIteration):
                iv_counts, iv_image = next(iter(interpreter.get_counterfactuals(image_id=image_id,
                                                                                image=image,
                                                                                shuffle=True)))
                iv_concept_counts.append(iv_counts)
                iv_predicted_classes.append(classifier.predict_single(image=iv_image))

    return concept_counts, predicted_classes, iv_concept_counts, iv_predicted_classes


@dataclass
class Experiment(NestedLogger):
    random_state: int
    repetitions: int
    images_url: str
    num_train_obs: int
    num_calibration_obs: int
    num_test_obs: int
    class_names: [str]
    type1: Type1Explainer
    type2: Type2Explainer
    model_agnostic_picker: Union[str, Any]
    param_grid: Dict[str, Any]
    cv_params: Dict[str, Any]
    top_k_acc: Iterable[int]
    observe_classifier_n_jobs: int

    def __post_init__(self):
        assert len(self.class_names) == self.classifier.num_classes, \
            'Provided number of class names differs from the output dimensionality of the classifier.'

    @property
    def classifier(self) -> TorchImageClassifier:
        return self.type2.classifier

    @property
    def interpreter(self) -> Interpreter:
        return self.type2.interpreter

    @property
    def params(self) -> Mapping[str, Any]:
        return {'classifier': self.classifier.name,
                'class_names': self.class_names,
                'images_url': self.images_url,
                'num_train_obs': self.num_train_obs,
                'num_calibration_obs': self.num_calibration_obs,
                'interpreter': str(self.interpreter),
                'concept_names': self.interpreter.concepts,
                'type2': str(self.type2),
                'type1': str(self.type1),
                'model_agnostic_picker': str(self.model_agnostic_picker),
                'num_test_obs': self.num_test_obs,
                'max_perturbed_area': self.interpreter.max_perturbed_area,
                'min_overlap_for_concept_merge': self.interpreter.max_concept_overlap}

    @property
    def is_multi_class(self) -> bool:
        return self.classifier.num_classes > 2

    @property
    def all_class_ids(self) -> [int]:
        return list(range(self.classifier.num_classes))

    def run(self, resume_at: int = 1):
        """
        Runs the configured number of repetitions of the experiment.
        For each repetition, yields a tuple *(surrogate, stats, fit_params, metrics)*.
        *surrogate* is the fitted surrogate model.
        *stats*, *fit_params*, and *metrics* are dicts that contain the augmentation statistics
        from the IPA algorithm, the hyperparameters of the fitted surrogate model and the experimental results.
        """
        assert resume_at >= 1

        with self.log_task('Running experiment...'):
            self.log_item('Parameters: {}'.format(self.params))

            with self.log_task('Caching test observations...'):
                test_obs = _prepare_test_observations(test_images_url=self.images_url,
                                                      interpreter=self.interpreter,
                                                      classifier=self.classifier,
                                                      num_test_obs=self.num_test_obs)
                self.counts_test, self.y_test, self.iv_counts_test, self.iv_y_test = test_obs

            skip_count = self.num_test_obs + (resume_at - 1) * self.num_train_obs
            with _prepare_image_iter(self.images_url, skip=skip_count) as train_images_iter:
                for rep_no in range(resume_at, self.repetitions + 1):
                    with self.log_task(f'Running repetition {rep_no}...'):
                        calibration_images_iter = it.islice(train_images_iter, self.num_calibration_obs)
                        with self.log_task('Calibrating the Type 2 explainer...'):
                            self.type2.calibrate(calibration_images_iter)

                        # we draw new train observations for each repetition by continuing the iterator
                        start = timeit.default_timer()
                        cv = ipa(type1=self.type1,
                                 type2=self.type2,
                                 model_agnostic_picker=self.model_agnostic_picker,
                                 image_iter=it.islice(train_images_iter,
                                                      self.num_train_obs),
                                 log_nesting=self.log_nesting,
                                 random_state=self.random_state,
                                 param_grid=self.param_grid,
                                 cv_params=self.cv_params,
                                 observe_classifier_n_jobs=self.observe_classifier_n_jobs)
                        stop = timeit.default_timer()

                        best_pipeline = cv.best_estimator_
                        picker = best_pipeline['interpret-pick']
                        agnostic_picker = best_pipeline['pick_agnostic']
                        surrogate = best_pipeline['approximate']

                        picked_concepts = picker.picked_concepts_
                        fit_params = {'picked_concepts_': picker.picked_concepts_,
                                      'picked_concept_names_': picker.picked_concept_names_,
                                      'concept_influences_': picker.concept_influences_}
                        if isinstance(agnostic_picker, SelectorMixin):
                            picked_concepts = np.asarray(picked_concepts)[agnostic_picker.get_support()].tolist()
                            fit_params['agnostic_picked_concepts_'] = agnostic_picker.get_support(indices=True).tolist()

                        with self.log_task('Scoring surrogate model...'):
                            metrics = self.score(surrogate, picked_concepts)

                        metrics['runtime_s'] = stop - start
                        yield (rep_no,
                               surrogate,
                               picker.stats_,
                               cv.best_params_,
                               fit_params,
                               metrics,
                               self.type1.serialize(surrogate))

    def _spread_probs_to_all_classes(self, probs, classes_) -> np.ndarray:
        """
        probs: list of probabilities, output of predict_proba
        classes_: classes that the classifier has seen during training (integer ids)

        Returns a list of probabilities indexed by *all* class ids,
        not only those that the classifier has seen during training.
        See https://stackoverflow.com/questions/30036473/
        """
        proba_ordered = np.zeros((probs.shape[0], len(self.class_names),), dtype=np.float)
        sorter = np.argsort(self.class_names)  # http://stackoverflow.com/a/32191125/395857
        idx = sorter[np.searchsorted(self.all_class_ids, classes_, sorter=sorter)]
        proba_ordered[:, idx] = probs
        return proba_ordered

    def _get_predictions(self, surrogate, counts: List[List[int]]):
        pred = surrogate.predict_proba(counts)
        pred = self._spread_probs_to_all_classes(pred, surrogate.classes_)
        if pred.shape[1] == 2:
            # binary case
            pred = pred[:, 1]
        return pred

    def score(self, surrogate, picked_concepts: [int]) -> Dict[str, Any]:
        metrics = self.type1.get_complexity_metrics(surrogate)

        picked_counts_test = np.asarray(self.counts_test)[:, picked_concepts]
        y_test_pred = self._get_predictions(surrogate, picked_counts_test)
        picked_iv_counts_test = np.asarray(self.iv_counts_test)[:, picked_concepts]
        iv_y_test_pred = self._get_predictions(surrogate, picked_iv_counts_test)
        metrics.update({'cross_entropy': log_loss(self.y_test, y_test_pred, labels=self.all_class_ids),
                        'auc': self._auc_score(self.y_test, y_test_pred),
                        'iv_cross_entropy': log_loss(self.iv_y_test, iv_y_test_pred, labels=self.all_class_ids),
                        'iv_auc': self._auc_score(self.iv_y_test, iv_y_test_pred)})
        for k in self.top_k_acc:
            metrics.update({f'top_{k}_acc': top_k_accuracy_score(surrogate=surrogate,
                                                                 counts=picked_counts_test,
                                                                 target_classes=self.y_test,
                                                                 k=k),
                            f'iv_top_{k}_acc': top_k_accuracy_score(surrogate=surrogate,
                                                                    counts=picked_iv_counts_test,
                                                                    target_classes=self.iv_y_test,
                                                                    k=k)})
        with _prepare_image_iter(self.images_url) as test_iter:
            images = list(it.islice(test_iter, self.num_test_obs))

        metrics.update(counterfactual_top_k_accuracy_metrics(surrogate=surrogate,
                                                             images=images,
                                                             counts=self.counts_test,
                                                             picked_concepts=picked_concepts,
                                                             target_classes=self.y_test,
                                                             classifier=self.classifier,
                                                             interpreter=self.interpreter,
                                                             k=1))
        return metrics

    def _auc_score(self, y_true, y_pred):
        multi_class = 'ovo' if self.is_multi_class else 'raise'
        auc = roc_auc_score(y_true,
                            y_pred,
                            average='macro',
                            multi_class=multi_class,
                            labels=self.all_class_ids)
        return auc


KEY_PARAMS = ('classifier',
              'images_url',
              'num_train_obs',
              'num_calibration_obs',
              'interpreter',
              'type2',
              'type1',
              'model_agnostic_picker',
              'max_perturbed_area',
              'min_overlap_for_concept_merge')


def _freeze(obj):
    if isinstance(obj, list):
        return tuple(obj)
    elif isinstance(obj, dict):
        if 'model_agnostic_picker' not in obj:  # this field is not present in older results
            obj['model_agnostic_picker'] = 'passthrough'
        return frozenset((k, _freeze(v)) for k, v in obj.items()
                         if k in KEY_PARAMS)
    else:
        return obj


def run_experiments(name: str,
                    description: str,
                    experiments: Iterable[Experiment],
                    prepend_timestamp: bool = True,
                    continue_previous_run: bool = False):
    if prepend_timestamp:
        timestamp = datetime.now().strftime('%Y-%m-%d-%H:%M:%S')
        name = timestamp + ' ' + name

    done_counter = Counter()
    exp_no_by_params = {}
    next_exp_no = 1
    with resources.path('ida.experiments', 'results') as results_dir:
        exp_dir = results_dir / name
        if continue_previous_run:
            assert exp_dir.exists()
            for e in get_experiment_dicts(name):
                params = _freeze(e['params'])
                # each parameter set must be identified by exactly one experiment number
                if params in exp_no_by_params:
                    assert exp_no_by_params[params] == e['exp_no']
                else:
                    next_exp_no = max(e['exp_no'] + 1, next_exp_no)
                    exp_no_by_params[params] = e['exp_no']
                done_counter.update([params])
        else:
            exp_dir.mkdir(exist_ok=False)  # fail if directory exists already
            with (exp_dir / 'description').open('w') as description_file:
                description_file.write(description)

    fields = ['exp_no', 'params', 'rep_no', 'stats', 'cv_params', 'fit_params', 'metrics', 'surrogate_serial']
    if continue_previous_run:
        with (exp_dir / 'results_packed.csv').open('r') as packed_csv_file:
            # check that csv has all required fields
            packed_reader = csv.DictReader(packed_csv_file)
            row = next(iter(packed_reader))
            assert set(fields) == set(row.keys()), 'The csv-file you want to append to has an unexpected set of fields!'
            fields = list(row.keys())  # use the field order from the existing csv file

    with (exp_dir / 'results_packed.csv').open('a+') as packed_csv_file:
        packed_writer = csv.DictWriter(packed_csv_file, fieldnames=fields)
        if not continue_previous_run:
            packed_writer.writeheader()

        for e in experiments:
            params = _freeze(e.params)

            if done_counter[params] >= e.repetitions:
                continue

            try:
                exp_no = exp_no_by_params[params]
            except KeyError:
                exp_no = next_exp_no
                exp_no_by_params[params] = exp_no
                next_exp_no += 1

            done_counter[params] += 1
            for result in e.run(resume_at=done_counter[params]):
                rep_no, surrogate, stats, cv_params, fit_params, metrics, serial = result
                packed_writer.writerow({'exp_no': exp_no,
                                        'params': e.params,
                                        'rep_no': rep_no,
                                        'stats': stats,
                                        'cv_params': cv_params,
                                        'fit_params': fit_params,
                                        'metrics': metrics,
                                        'surrogate_serial': serial})
                packed_csv_file.flush()  # make sure intermediate results are written


def get_experiment_dicts(experiment_name: str) -> Iterable[Dict[str, Any]]:
    with resources.path('ida.experiments.results', experiment_name) as path:
        with (path / 'results_packed.csv').open('r') as results_csv_file:
            for row in csv.DictReader(results_csv_file):
                packed = {}
                for k, v in row.items():
                    # fix a parsing error with some older results
                    v = re.sub(r'(?:array)\((\[.*\])(?:,.*\))',
                               repl=r'\1',
                               string=v,
                               flags=re.DOTALL)
                    assert 'array' not in v
                    try:
                        packed[k] = ast.literal_eval(v)
                    except ValueError:
                        pass
                yield packed


def get_experiment_df(experiment_name: str) -> pd.DataFrame:
    unpacked_rows = []
    unpacked_column_names = set()
    for packed in get_experiment_dicts(experiment_name):
        unpacked = {}
        for k, v in packed.items():
            if isinstance(v, dict):
                unpacked_column_names.update(v.keys())
                unpacked.update(v)
            else:
                unpacked.update({k: v})
        unpacked_rows.append(unpacked)

    unified_rows = []
    for row in unpacked_rows:
        d = {c: None for c in unpacked_column_names}
        d.update(row)
        unified_rows.append(d)

    return pd.DataFrame(unified_rows)


def get_experiment_row(experiment_name: str, exp_no: int, rep_no: int):
    df = get_experiment_df(experiment_name)
    return df.loc[df['exp_no'] == exp_no].loc[df['rep_no'] == rep_no].iloc[0]

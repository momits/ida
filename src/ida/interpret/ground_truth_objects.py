from typing import Optional, Tuple, Iterable

import numpy as np

from ida.interpret.common import Interpreter
from ida.common import ImageObjectProvider


class GroundTruthObjectsInterpreter(Interpreter):
    """
    Describes each image with a set of masks for ground-truth objects,
    taken from an `ImageObjectProvider`.
    """

    def __init__(self,
                 gt_object_provider: ImageObjectProvider,
                 subset: Optional[str] = None,
                 ignore_images_without_objects: bool = True,
                 random_state: int = 42,
                 max_concept_overlap: float = .4,
                 max_perturbed_area: float = .6):
        """
        :param gt_object_provider: provider of object bounding boxes for given image ids
        :param subset: optional subset of the dataset for which the image ids are unique
        :param ignore_images_without_objects: whether to ignore or fail when an image
            has no assigned object bounding boxes
        :param random_state to use
        """
        super().__init__(random_state=random_state,
                         max_concept_overlap=max_concept_overlap,
                         max_perturbed_area=max_perturbed_area)
        self.gt_object_provider = gt_object_provider
        self.subset = subset
        self.ignore_images_without_objects = ignore_images_without_objects

    def __str__(self):
        return 'ground_truth_objects'

    @property
    def concepts(self):
        return self.gt_object_provider.object_names

    def get_implied_concepts(self, concept_id: int) -> Iterable[int]:
        return self.gt_object_provider.get_implied_objects(concept_id)

    def __call__(self, image: Optional[np.ndarray], image_id: Optional[str],
                 image_size: Optional[Tuple[int, int]] = None, **kwargs) -> Iterable[Tuple[int, np.ndarray]]:
        assert image_id is not None, ('This interpreter requires image ids in order to look up bounding boxes from '
                                      'the ground truth objects provider.')
        if image is not None:
            height, width = image.shape[:2]  # image has shape H, W, C
        elif image_size is not None:
            height, width = image_size
        else:
            raise ValueError('This interpreter requires either the `image` or the `image_size` argument '
                             'to compute the absolute coordinates of objects on the image.')

        try:
            boxes = list(self.gt_object_provider.get_object_bounding_boxes(image_id, self.subset))
        except KeyError as e:
            if self.ignore_images_without_objects:
                return []
            raise e

        for mask_no, (object_id, x_0, y_0, x_1, y_1) in enumerate(boxes):
            mask = np.zeros((height, width), dtype=np.bool_)
            y_0 = np.clip(y_0 * height, 0, height).astype(int)
            y_1 = np.ceil(np.clip(y_1 * height, 0, height)).astype(int)
            x_0 = np.clip(x_0 * width, 0, width).astype(int)
            x_1 = np.ceil(np.clip(x_1 * width, 0, width)).astype(int)
            mask[y_0:y_1, x_0:x_1] = True
            yield object_id, mask

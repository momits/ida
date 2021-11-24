from typing import Type, Optional, Tuple, Iterable, List

import numpy as np
import torch
from captum.attr import Attribution, Saliency, IntegratedGradients, DeepLift, GuidedGradCam

from liga.interpret.common import Interpreter
from liga.type2.common import Type2Explainer, CaptumAttributionWrapper
from liga.torch_extensions.classifier import TorchImageClassifier


class GradientAttributionType2Explainer(Type2Explainer):

    def __init__(self,
                 classifier: TorchImageClassifier,
                 interpreter: Interpreter,
                 gradient_attribution_method: Type[Attribution],
                 quantile_level: float,
                 takes_additional_attribution_args: bool = False,
                 **additional_init_args):
        super().__init__(classifier=classifier,
                         interpreter=interpreter)
        self.captum_wrapper = CaptumAttributionWrapper(classifier=classifier,
                                                       attribution_method=gradient_attribution_method,
                                                       **additional_init_args)
        self.takes_additional_attribution_args = takes_additional_attribution_args
        self.quantile_level = quantile_level
        self._pixel_influence_sum = 0
        self._pixel_count = 0
        self.quantile = None

    @property
    def stats(self):
        return {'mean_pixel_influence': self._pixel_influence_sum / self._pixel_count,
                'quantile': self.quantile}

    def calibrate(self, images_iter: Iterable[Tuple[str, np.ndarray]], **kwargs):
        calibration_influences = []
        for image_id, image in images_iter:
            for _, _, raw_influences in self._get_raw_influences(image=image,
                                                                 image_id=image_id,
                                                                 **kwargs):
                calibration_influences.extend(raw_influences)
        self.quantile = np.quantile(np.abs([i for i in calibration_influences if i > 0]),
                                    self.quantile_level)

    def _get_raw_influences(self, image: np.ndarray, image_id: Optional[str] = None, **kwargs) \
            -> Iterable[Tuple[int, np.ndarray, List[float]]]:
        feature_influences = None

        for concept_id, mask in self.interpreter(image=image,
                                                 image_id=image_id,
                                                 **kwargs):
            if not self.takes_additional_attribution_args:
                kwargs = {}

            if feature_influences is None:
                feature_influences = (self.captum_wrapper(image=image,
                                                          additional_attribution_args=kwargs)
                                      .transpose(1, 2, 0))  # CxHxW -> HxWxC

            self._pixel_count += np.count_nonzero(mask)
            self._pixel_influence_sum += np.sum(feature_influences)

            influences_at_concept = feature_influences[mask].flatten().tolist()
            yield concept_id, mask, influences_at_concept

    def __call__(self, image: np.ndarray, image_id: Optional[str] = None, **kwargs) \
            -> Iterable[Tuple[int, np.ndarray, float]]:
        assert self.quantile is not None, 'The explainer must be calibrated before use.'
        for concept_id, mask, influences_at_concept in self._get_raw_influences(image=image,
                                                                                image_id=image_id,
                                                                                **kwargs):
            if np.mean(np.abs(influences_at_concept)) < self.quantile:
                influential = 0.
            else:
                influential = 1.

            yield concept_id, mask, influential

    def __str__(self):
        return '{}(quantile_level={})'\
            .format(type(self).__name__, self.quantile_level)


class SaliencyType2Explainer(GradientAttributionType2Explainer):
    def __init__(self,
                 classifier: TorchImageClassifier,
                 interpreter: Interpreter,
                 quantile_level: float):
        super().__init__(classifier=classifier,
                         interpreter=interpreter,
                         gradient_attribution_method=Saliency,
                         takes_additional_attribution_args=True,
                         quantile_level=quantile_level)

    def __call__(self, image: np.ndarray, image_id: Optional[str] = None, **kwargs) -> [float]:
        return super().__call__(image=image,
                                image_id=image_id,
                                abs=False)  # we want positive *and* negative attribution values
        # we ignore kwargs here because the Saliency class cannot deal with them


class IntegratedGradientsType2Explainer(GradientAttributionType2Explainer):
    def __init__(self,
                 classifier: TorchImageClassifier,
                 interpreter: Interpreter,
                 quantile_level: float):
        super().__init__(classifier=classifier,
                         interpreter=interpreter,
                         gradient_attribution_method=IntegratedGradients,
                         quantile_level=quantile_level)


class DeepLiftType2Explainer(GradientAttributionType2Explainer):
    def __init__(self,
                 classifier: TorchImageClassifier,
                 interpreter: Interpreter,
                 quantile_level: float):
        super().__init__(classifier=classifier,
                         interpreter=interpreter,
                         gradient_attribution_method=DeepLift,
                         quantile_level=quantile_level)


class GuidedGradCamType2Explainer(GradientAttributionType2Explainer):
    def __init__(self,
                 classifier: TorchImageClassifier,
                 interpreter: Interpreter,
                 layer: torch.nn.Module,
                 quantile_level: float):
        super().__init__(classifier=classifier,
                         interpreter=interpreter,
                         gradient_attribution_method=GuidedGradCam,
                         quantile_level=quantile_level,
                         layer=layer)

    def __call__(self, image: np.ndarray, image_id: Optional[str] = None, **kwargs) -> [float]:
        return super().__call__(image=image,
                                image_id=image_id)
        # we ignore kwargs here because the GuidedGradCam class cannot deal with them

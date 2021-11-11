import json
import logging
from importlib import resources
from pathlib import Path
from typing import Optional, Tuple
from unittest import mock

import numpy as np
import torch
import torchvision
from skimage import img_as_float
from skimage.transform import resize
from torch.nn import DataParallel
from torch.nn.functional import softmax

from simexp.common import Classifier
from simexp.describe.torch_based.common import TorchConfig
from liga.torch_extensions.adjusted_resnet_basic_block import AdjustedBasicBlock
from simexp.util.webcache import WebCache


_COLLECTION_PACKAGE = 'liga.classifier_collection.torch'


class TorchImageClassifier(Classifier):

    def __init__(self,
                 name: str,
                 num_classes: str,
                 param_url: Optional[str],
                 is_parallel: bool,
                 torch_cfg: TorchConfig,
                 input_size: Tuple[int, int] = (224, 224),
                 cache: WebCache = WebCache(),
                 is_latin1: bool = False):
        """

        :param name: name of this model
        :param num_classes: how many classes this classifier discriminates
        :param param_url: an optional file to load this model from.
            if None, the model will be loaded from the torchvision collection.
        :param is_parallel: whether this model uses `DataParallel`
        :param torch_cfg: torch_cfg: TorchConfig
        :param input_size: required input size of the model
        :param cache: WebCache = WebCache()
        :param is_latin1: whether this model was encoded with latin1 (a frequent "bug" with models exported from older torch versions)
        """
        super().__init__(name=name, num_classes=num_classes)

        self.param_url = param_url
        self.is_parallel = is_parallel
        self.torch_cfg = torch_cfg
        self.input_size = input_size
        self.cache = cache
        self.is_latin1 = is_latin1

        if self.param_url:
            self.url, self.file_name = self.param_url.rsplit('/', 1)
            self.url += '/'

        self._model = None

        # magic constants in the torch community
        means, sds = np.asarray([0.485, 0.456, 0.406]), np.asarray([0.229, 0.224, 0.225])
        self._means_nested = means[None, None, :]
        self._sds_nested = sds[None, None, :]

    @staticmethod
    def from_json_file(path: str, torch_cfg: TorchConfig):
        with resources.path(_COLLECTION_PACKAGE, path) as path:
            with path.open('r') as f:
                json_dict = json.load(f)
                return TorchImageClassifier(name=json_dict['name'],
                                            param_url=json_dict['param_url'],
                                            is_parallel=json_dict['is_parallel'],
                                            num_classes=json_dict['num_classes'],
                                            is_latin1=json_dict['is_latin1'],
                                            torch_cfg=torch_cfg)

    def _convert_latin1_to_unicode_and_cache(self):
        dest_path = self.cache.cache_dir / self.file_name
        legacy_path = Path(self.cache.cache_dir / (dest_path.name + '.legacy'))

        if not dest_path.exists():
            logging.info('Converting legacy latin1 encoded model to unicode...', )
            with self.cache.open(self.file_name, self.url, legacy_path.name, 'rb') as legacy_tar_file:
                model = torch.load(legacy_tar_file,
                                   map_location=self.torch_cfg.device,
                                   encoding='latin1')
            torch.save(model, dest_path)
            legacy_path.unlink()
            logging.info('Conversion done.')

    @property
    def torch_model(self):
        with mock.patch.object(torchvision.models.resnet, 'BasicBlock', AdjustedBasicBlock):
            if self.torch_cfg is None:
                raise RuntimeError('Attribute torch_cfg must be set before the model can be accessed.')

            if self._model is not None:
                return self._model

            if self.param_url is None:
                self._model = torchvision.models.__dict__[self.name](pretrained=True)
            else:
                if self.is_latin1:
                    self._convert_latin1_to_unicode_and_cache()

                with self.cache.open(self.file_name, self.url, None, 'rb') as param_file:
                    checkpoint = torch.load(param_file, map_location=self.torch_cfg.device)

                if type(checkpoint).__name__ == 'OrderedDict' or type(checkpoint).__name__ == 'dict':
                    self._model = torchvision.models.__dict__[self.name](num_classes=self.num_classes)
                    if self.is_parallel:
                        # the data parallel layer will add 'module' before each layer name:
                        state_dict = {str.replace(k, 'module.', ''): v
                                      for k, v in checkpoint['state_dict'].items()}
                    else:
                        state_dict = checkpoint
                    self._model.load_state_dict(state_dict)
                else:
                    self._model = checkpoint

        self._model.eval()
        if self.is_parallel and torch.cuda.device_count() > 1:
            self._model = DataParallel(self._model)
        return self._model

    def preprocess(self, image: np.ndarray) -> np.ndarray:
        """
        Takes in an RGB `image` in shape (H, W, C) with value range [0, 255]
        and outputs a resized and normalized image in shape (C, H, W).
        """
        image = img_as_float(image)
        image = (resize(image, self.input_size) - self._means_nested) / self._sds_nested
        return np.transpose(image, (2, 0, 1))

    def predict_proba(self, inputs: np.ndarray, preprocessed=False) -> np.ndarray:
        if not preprocessed:
            inputs = np.asarray([self.preprocess(img) for img in inputs])

        self.torch_model.to(self.torch_cfg.device)
        image_tensors = torch.from_numpy(inputs).float().to(self.torch_cfg.device)
        logits = self.torch_model(image_tensors)
        probs = softmax(logits, dim=1)
        return probs.detach().cpu().numpy()

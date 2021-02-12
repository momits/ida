import abc
import logging
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Optional, Tuple, Iterable
from unittest import mock

import numpy as np
import torch
import torchvision
from simple_parsing import Serializable
from skimage import img_as_float
from skimage.transform import resize
from torch.nn import DataParallel
from torch.nn.functional import softmax

from simadv.common import Classifier, RowDict
from simadv.describe.common import DictBasedImageDescriber, ImageReadConfig
from simadv.describe.torch_based.adjusted_resnet_basic_block import AdjustedBasicBlock
from simadv.util.webcache import WebCache


@dataclass
class TorchConfig:
    use_cuda: bool = True  # whether to use CUDA if available

    def __post_init__(self):
        self.use_cuda = self.use_cuda and torch.cuda.is_available()
        self.device: torch.device = torch.device('cuda') if self.use_cuda else torch.device('cpu')

    def set_device(self):
        """
        Context manager that executes the contained code with the
        configured torch device.
        """
        return torch.cuda.device(self.device if self.use_cuda else -1)


@dataclass
class TorchImageClassifier(Classifier, Serializable):

    # an optional file to load this model from
    # if None, the model will be loaded from the torchvision collection.
    param_url: Optional[str]

    # whether this model uses `DataParallel`
    is_parallel: bool

    # torch configuration to use
    torch_cfg: TorchConfig

    # required input size of the model
    input_size: Tuple[int, int] = (224, 224)

    # where to cache downloaded information
    cache: WebCache = WebCache()

    # whether this model was encoded with latin1 (a frequent "bug" with models exported from older torch versions)
    is_latin1: bool = False

    def __post_init__(self):
        if self.param_url:
            self.url, self.file_name = self.param_url.rsplit('/', 1)
            self.url += '/'

        self._model = None

        # magic constants in the torch community
        means, sds = np.asarray([0.485, 0.456, 0.406]), np.asarray([0.229, 0.224, 0.225])
        self._means_nested = means[None, None, :]
        self._sds_nested = sds[None, None, :]

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


@dataclass
class TorchImageClassifierSerialization:
    path: str  # path to a json serialization of a TorchImageClassifier

    _COLLECTION_PACKAGE = 'simadv.classifier_collection.torch'

    def _raise_invalid_path(self):
        raise ValueError('Classifier path {} is neither a valid path, nor does it point to a valid resource.'
                         .format(self.path))

    def __post_init__(self):
        if not Path(self.path).exists():
            try:
                if self.path.endswith('.json') and resources.is_resource(self._COLLECTION_PACKAGE, self.path):
                    with resources.path(self._COLLECTION_PACKAGE, self.path) as path:
                        self.path = str(path)
                else:
                    self._raise_invalid_path()
            except ModuleNotFoundError:
                self._raise_invalid_path()


@dataclass
class BatchedTorchImageDescriber(DictBasedImageDescriber, abc.ABC):
    """
    Reads batches of image data from a petastorm store
    and converts these images to Torch tensors.

    Attention: If the input images can have different sizes, you *must*
    set `read_cfg.batch_size` to 1!

    Subclasses can describe the produced tensors as other data
    batch-wise by implementing the `describe_batch` method.
    """

    read_cfg: ImageReadConfig
    torch_cfg: TorchConfig

    def batch_iter(self):
        def to_tensor(batch_columns):
            row_ids, image_arrays = batch_columns
            image_tensors = torch.as_tensor(image_arrays) \
                .to(self.torch_cfg.device, torch.float) \
                .div(255)

            return row_ids, image_tensors

        current_batch = []

        for row in self.read_cfg.make_reader(None):
            current_batch.append((row.image_id, row.image))

            if len(current_batch) < self.read_cfg.batch_size:
                continue

            yield to_tensor(list(zip(*current_batch)))
            current_batch.clear()

        if current_batch:
            yield to_tensor(list(zip(*current_batch)))

    @abc.abstractmethod
    def describe_batch(self, ids, batch) -> Iterable[RowDict]:
        pass

    def clean_up(self):
        """
        Can be overridden to clean up more stuff.
        """
        torch.cuda.empty_cache()  # release all memory that can be released

    def generate(self):
        with torch.no_grad():
            for ids, batch in self.batch_iter():
                yield from self.describe_batch(ids, batch)

        self.clean_up()
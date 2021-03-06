from dataclasses import dataclass, field

from petastorm.unischema import Unischema
from simple_parsing import ArgumentParser

from ida.common import LoggingConfig
from simexp.observe_classifier.common import ClassifierObservationGenerator
from simexp.spark import PetastormWriteConfig
from ida.torch_extensions.classifier import TorchImageClassifierLoader


@dataclass
class TorchClassifierObservationGenerator(TorchImageClassifierLoader, ClassifierObservationGenerator):

    def __post_init__(self):
        super().__post_init__()  # super classes do stuff for us here


@dataclass
class ConceptsWriteConfig(PetastormWriteConfig):
    output_schema: Unischema = field(default=None, init=False)


if __name__ == '__main__':
    parser = ArgumentParser(description='Generate test data for surrogate models of a torch image classifier.')
    parser.add_arguments(TorchClassifierObservationGenerator, dest='generator')
    parser.add_arguments(ConceptsWriteConfig, dest='write_cfg')
    parser.add_arguments(LoggingConfig, dest='logging')
    args = parser.parse_args()

    generator: TorchClassifierObservationGenerator = args.generator
    write_cfg: PetastormWriteConfig = args.write_cfg
    write_cfg.output_schema = generator.output_schema

    generator.spark_cfg.write_petastorm(generator.to_df(), write_cfg)

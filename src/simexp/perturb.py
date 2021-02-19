import abc
from dataclasses import dataclass, field
from functools import reduce
from typing import Iterable, Tuple, Any, Iterator, List

import numpy as np
import itertools as it

from petastorm.codecs import ScalarCodec
from petastorm.unischema import Unischema, UnischemaField
from pyspark.sql import DataFrame
import pyspark.sql.functions as sf
import pyspark.sql.types as st
from simple_parsing import ArgumentParser

from simexp.common import RowDict, LoggingConfig
from simexp.spark import Field, SparkSessionConfig, DictBasedDataGenerator, PetastormWriteConfig


class Perturber(abc.ABC):

    @abc.abstractmethod
    def perturb(self, influential_counts: np.ndarray, counts: np.ndarray, sampler: Iterable[Tuple[np.ndarray, Any]]) \
            -> Iterable[Tuple[np.ndarray, Any]]:
        """
        Takes in two arrays `counts` and `influential_counts` of the same dimension 1xO,
        where O is the number of objects in a classification task.
        `counts` are object counts on an image, and `influential_counts` represents a subset of these objects.
        This subset comprises objects that one deems influential for the classification of this image.

        From this subset the method derives alternative count arrays that by expectation
        all yield the same prediction as `count`.
        The method can use the `sampler` to draw random object count arrays together with their image id
        from the same image distribution that `count` was derived from.

        :return tuples of count arrays and image ids. if a count array was derived from an image drawn from `sampler`,
            the corresponding image id must be returned, else None.
        """


@dataclass
class LocalPerturber(Perturber):
    """
    Assumes that given "influential objects" on an image are a locally sufficient condition for its classification,
    i.e., for images that are similar.
    Derives object counts for "similar" images by dropping all other, "non-influential" objects from the given image.
    Generates all combinations of dropped object counts if they are less than `max_perturbations`, else generates
    `max_perturbations` combinations randomly.
    """

    # upper bound for perturbations to generate
    max_perturbations: int = 10

    def perturb(self, influential_counts: np.ndarray, counts: np.ndarray, sampler: Iterable[Tuple[np.ndarray, Any]]) \
            -> Iterable[Tuple[np.ndarray, Any]]:
        droppable_counts = counts - influential_counts

        if np.multiply.reduce(droppable_counts + 1) > self.max_perturbations:
            yield from self._perturb_random(counts, droppable_counts)
        else:
            yield from self._perturb_exhaustive(counts, droppable_counts)

    @staticmethod
    def _perturb_exhaustive(counts, droppable_counts):
        gens = []
        for droppable_index in np.flatnonzero(droppable_counts):
            gens.append(zip(range(0, droppable_counts[droppable_index] + 1), it.repeat(droppable_index)))

        for drops in it.product(*gens):
            perturbed = counts.copy()
            for drop_count, drop_index in drops:
                perturbed[drop_index] -= drop_count
            yield perturbed, None

    def _perturb_random(self, counts, droppable_counts):
        for _ in range(self.max_perturbations):
            perturbed = counts.copy()
            for drop_index in np.flatnonzero(droppable_counts):
                drop_count = np.random.randint(droppable_counts[drop_index] + 1)
                perturbed[drop_index] -= drop_count
            yield perturbed, None


@dataclass
class GlobalPerturber(Perturber):
    """
    Assumes that given "influential objects" on an image are a globally sufficient condition for its classification.
    Hence replaces all other objects randomly with objects from other images from the same distribution,
    and assumes that the classification stays the same.
    """

    # how many perturbed object counts to generate for each image
    num_perturbations: int = 10

    def perturb(self, influential_counts: np.ndarray, counts: np.ndarray, sampler: Iterable[Tuple[np.ndarray, Any]]) \
            -> Iterable[Tuple[np.ndarray, Any]]:
        """
        Returns the original counts array plus `num_perturbations` (class parameter) additional arrays.
        The latter are unions of `influential_counts` with random counts drawn from `sampler`.
        """
        # original counts
        yield counts, None

        if np.any(influential_counts):
            # sample random object counts from the same distribution
            for sample_counts, sample_id in it.islice(sampler, self.num_perturbations):
                # keep all influential objects from the original image,
                # change the rest based on the sample → pairwise maximum of counts
                combined_counts = np.maximum(influential_counts, sample_counts)
                yield combined_counts, sample_id


class InfluenceDetector(abc.ABC):

    @abc.abstractmethod
    def detect(self, influence_mask: np.ndarray, concept_mask: np.ndarray) -> bool:
        """
        Decides whether the mask defined by the boolean matrix `concept_mask`
        is influential for a classification according to the influential pixels
        defined by the float matrix `influence_mask`.
        """


@dataclass
class LiftInfluenceDetector(InfluenceDetector):

    # a concept is considered relevant for the prediction of the classifier
    # if the sum of influence values falling into the mask of the concept
    # exceeds the fraction 'concept area' / 'image area' by a factor `lift_threshold`.
    lift_threshold: float = 1.5

    def detect(self, influence_mask: np.ndarray, concept_mask: np.ndarray) -> bool:
        height, width = influence_mask.shape
        img_area = float(height * width)

        mask_area = float(np.count_nonzero(concept_mask))
        # lift = how much more influence than expected do pixels of the mask have?
        lift = np.sum(influence_mask[concept_mask]) / (mask_area / img_area)
        return lift > self.lift_threshold


@dataclass
class PerturbedConceptCountsGenerator(DictBasedDataGenerator):

    # name of this generator
    name: str = field(default='perturbed_concept_counts', init=False)

    # the output schema of this data generator.
    output_schema: Unischema = field(init=False)

    # how to use spark
    spark_cfg: SparkSessionConfig

    # url of petastorm parquet store of schema `Schema.PIXEL_INFLUENCES`
    influences_url: str

    # urls of petastorm parquet stores of schema `Schema.CONCEPT_MASKS`
    concept_mask_urls: List[str]

    # which detectors to use
    detectors: List[InfluenceDetector]

    # which perturbers to use
    perturbers: List[Perturber]

    def __post_init__(self):
        @sf.udf(st.ArrayType(st.StringType()))
        def unique_concept_names(describer_name, concept_names):
            return (describer_name + '.' + np.char.asarray(concept_names).astype(np.unicode_)).tolist()

        # make concept names unique
        self.union_df = self._get_union_of_describers_df() \
            .withColumn('tmp', unique_concept_names(Field.DESCRIBER.name,
                                                    Field.CONCEPT_NAMES.name)) \
            .drop(Field.DESCRIBER.name, Field.CONCEPT_NAMES.name) \
            .withColumnRenamed('tmp', Field.CONCEPT_NAMES.name)

        flatten_agg = sf.flatten(sf.collect_list(sf.col(Field.CONCEPT_NAMES.name)))
        all_concept_names_df = self.union_df.agg(flatten_agg).distinct()
        self.all_concept_names = [row[Field.CONCEPT_NAMES.name] for row in all_concept_names_df.collect()]

        influence_fields = [Field.IMAGE_ID, Field.INFLUENCE_ESTIMATOR, Field.PERTURBER,
                            Field.DETECTOR, Field.PERTURBED_IMAGE_ID]
        concept_fields = [UnischemaField(concept_name, np.uint8, (), ScalarCodec(st.IntegerType), False)
                          for concept_name in self.all_concept_names]
        self.output_schema = Unischema('PerturbedConceptCounts', influence_fields + concept_fields)

    def sampler(self):
        assert hasattr(self, 'union_df')
        while True:
            sampled_row = self.union_df.rdd.takeSample(True, 1).asDict()
            counts = np.zeros((self.all_concept_names,), dtype=np.uint8)
            for concept_name in sampled_row[Field.CONCEPT_NAMES.name]:
                counts[self.all_concept_names.index(concept_name)] += 1
            yield counts, Field.IMAGE_ID.decode(sampled_row[Field.IMAGE_ID.name])

    def _get_union_of_describers_df(self):
        return reduce(DataFrame.union, [self.spark_cfg.session.read.parquet(url) for url in self.concept_mask_urls])

    def generate(self) -> Iterator[RowDict]:
        influences_df = self.spark_cfg.session.read.parquet(self.influences_url)
        per_image_df = self.union_df.join(influences_df, on=Field.IMAGE_ID.name, how='inner') \
            .groupBy(Field.IMAGE_ID.name) \
            .agg({'*': 'collect_list'})

        for per_image_row in per_image_df.collect():
            image_id = Field.IMAGE_ID.decode(per_image_row[Field.IMAGE_ID.name])

            counts = np.zeros((len(self.all_concept_names, )), dtype=np.uint8)

            # each image has multiple arrays of concept names from different describers
            for concept_names in per_image_row[Field.CONCEPT_NAMES.name]:
                for concept_name in concept_names:
                    counts[self.all_concept_names.index(concept_name)] += 1

            # classification should be constant for the same image
            predicted_classes = set(per_image_row[Field.PREDICTED_CLASS.name])
            assert len(predicted_classes) == 1
            predicted_class = predicted_classes.pop()

            # yield the classification of the image, together with the unperturbed concept counts
            # this is the baseline on which influence estimators strive to improve
            yield {Field.PREDICTED_CLASS.name: predicted_class,
                   Field.INFLUENCE_ESTIMATOR.name: None,
                   Field.PERTURBER.name: None,
                   Field.DETECTOR.name: None,
                   Field.IMAGE_ID.name: image_id,
                   Field.PERTURBED_IMAGE_ID: None,
                   **dict(zip(self.all_concept_names, counts))}

            # each image has multiple pixel influence masks from different estimators
            for influence_estimator, influence_mask in zip(per_image_row[Field.INFLUENCE_ESTIMATOR.name],
                                                           per_image_row[Field.INFLUENCE_MASK.name]):
                influence_estimator = Field.INFLUENCE_ESTIMATOR.decode(influence_estimator)
                influence_mask = Field.INFLUENCE_MASK.decode(influence_mask)

                for detector in self.detectors:
                    influential_counts = np.zeros((len(self.all_concept_names),), dtype=np.uint8)
                    for concept_names, concept_masks in zip(per_image_row[Field.CONCEPT_NAMES.name],
                                                            map(Field.CONCEPT_MASKS.decode,
                                                                per_image_row[Field.CONCEPT_MASKS.name])):
                        for concept_name, concept_mask in zip(concept_names, concept_masks):
                            if detector.detect(influence_mask, concept_mask):
                                influential_counts[self.all_concept_names.index(concept_name)] += 1
                                self._log_item('[{}] Concept {} has exceptional influence.'
                                               .format(influence_estimator, concept_name))

                    for perturber in self.perturbers:
                        for perturbed_counts, perturbed_image_id \
                                in perturber.perturb(influential_counts, counts, self.sampler()):
                            yield {Field.PREDICTED_CLASS.name: predicted_class,
                                   Field.INFLUENCE_ESTIMATOR.name: influence_estimator,
                                   Field.PERTURBER.name: perturber,
                                   Field.DETECTOR.name: detector,
                                   Field.IMAGE_ID.name: image_id,
                                   Field.PERTURBED_IMAGE_ID: perturbed_image_id,
                                   **dict(zip(self.all_concept_names, perturbed_counts))}


@dataclass
class CLInterface(PerturbedConceptCountsGenerator):
    # how to use spark
    spark_cfg: SparkSessionConfig

    # url of petastorm parquet store of schema `Schema.PIXEL_INFLUENCES`
    influences_url: str

    # urls of petastorm parquet stores of schema `Schema.CONCEPT_MASKS`
    concept_mask_urls: List[str]

    # which detectors to use
    detectors: List[InfluenceDetector] = field(default_factory=list, init=False)

    # which perturbers to use
    perturbers: List[Perturber] = field(default_factory=list, init=False)

    # thresholds to run detector with
    detection_thresholds: List[float]

    # how many perturbed object counts to generate per image using the `GlobalPerturber`
    global_perturbations_per_image: List[int]

    # how many perturbed object counts to generate per image using the `LocalPerturber`
    max_local_perturbations_per_image: List[int]

    def __post_init__(self):
        detectors = [LiftInfluenceDetector(threshold) for threshold in self.detection_thresholds]
        perturbers = [GlobalPerturber(num_perturbations)
                      for num_perturbations in self.global_perturbations_per_image]
        perturbers += [LocalPerturber(max_perturbations)
                       for max_perturbations in self.max_local_perturbations_per_image]
        self.generator = PerturbedConceptCountsGenerator(self.spark_cfg, self.time_limit_s, self.influences_url,
                                                         self.concept_mask_urls, detectors, perturbers)


if __name__ == '__main__':
    parser = ArgumentParser(description='Leverage pixel influence estimators, such as LIME or DeepLift,'
                                        'to improve the training data for surrogate models of an image classifier.'
                                        'The generated training data is not in the pixel space used by the classifier,'
                                        'but in the space of "concept-counts" on images.')
    parser.add_arguments(CLInterface, dest='generator')
    parser.add_arguments(LoggingConfig, dest='logging')
    parsed, remaining = parser.parse_known_args()
    generator: PerturbedConceptCountsGenerator = parsed.generator

    parser = ArgumentParser()

    @dataclass
    class ConceptsWriteConfig(PetastormWriteConfig):
        output_schema: Unischema = field(default_factory=lambda: generator.output_schema, init=False)

    parser.add_arguments(ConceptsWriteConfig, dest='write_cfg')
    parsed = parser.parse_args(remaining)
    write_cfg: PetastormWriteConfig = parsed.write_cfg

    generator.spark_cfg.write_petastorm(generator.to_df(), write_cfg)
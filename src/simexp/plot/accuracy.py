import re
from dataclasses import dataclass
from typing import Tuple

import pandas as pd
from plotnine import *

from simexp.fit import SurrogatesFitter

clear_theme = (theme_bw() +
               theme(panel_border=element_blank(),
                     panel_grid=element_blank(),
                     panel_grid_major=element_blank(),
                     panel_grid_minor=element_blank(),
                     text=element_text(wrap=True,
                                       family='Latin Modern Roman',
                                       fontstretch='normal',
                                       fontweight='light',
                                       size=16,
                                       colour='black'),
                     plot_title=element_text(size=20, fontweight='normal'),
                     axis_title=element_text(size=14, fontweight='normal'),
                     line=element_line(colour='black', size=.5),
                     axis_ticks=element_blank(),
                     strip_text_x=element_text(size=10),
                     strip_background=element_blank(),
                     legend_key=element_blank()))


@dataclass
class SurrogatesResultPlotter:

    # the results to plot
    results: SurrogatesFitter.Results

    @property
    def df(self) -> pd.DataFrame:
        return self.results.to_flat_pandas()

    @staticmethod
    def _get_ie_name_and_params(influence_estimator_name) -> Tuple[str, str]:
        name, params = re.search(r'^(.*)InfluenceEstimator\((.*)\)$', influence_estimator_name)[:2]
        return name, params

    def plot_best_accuracy_per_influence_estimator(self):
        max_indices = self.df.groupby(by='influence_estimator')['top_k_accuracy'].idxmax()
        df = self.df.loc[max_indices]
        df['hyperparameters'] = df.apply(lambda x: 'No perturbation' if x.perturber == 'none'
                                         else '{},\n{}'.format(x.perturber, x.detector), axis=1)
        assert df['top_k'].nunique() == 1, 'cannot merge top-k accuracies with different k'
        k = df['top_k'][0]

        name_params_df = df['influence_estimator'] \
            .str.extract(r'^(?P<influence_estimator_name>.*)InfluenceEstimator'
                         r'\((?P<influence_estimator_params>.*)\)$',
                         expand=True) \
            .fillna('None')
        df = pd.concat([df, name_params_df], axis=1)

        return (ggplot(df, aes('influence_estimator_name')) +
                clear_theme +
                geom_col(aes(y='top_k_accuracy', fill='hyperparameters')) +
                ggtitle('Best Top-{}-Accuracy Per Influence Estimator'.format(k)) +
                labs(x='Pixel Influence Estimator', fill='Perturbation parameters') +
                theme(axis_title_x=element_blank(),
                      axis_title_y=element_blank(),
                      axis_text_x=element_text(angle=-45, hjust=0, vjust=1),
                      legend_title=element_text(margin={'b', 10}),
                      legend_entry_spacing=5) +
                scale_fill_brewer(type='qual', palette='Paired'))

    def plot_accuracy_by_perturb_fraction(self):
        df = self.df
        df.assign(hyperparameters=lambda x: '{}, {}, {}'.format(x.influence_estimator, x.perturber, x.detector))

        return (ggplot(df, aes('perturb_fraction')) +
                clear_theme +
                geom_path(aes(y='cross_entropy', fill='hyperparameters')) +
                facet_wrap(['train_sample_fraction']) +
                ggtitle('Accuracy per Fraction of Perturbed Images'))

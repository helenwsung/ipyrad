#!/usr/bin/env python

"""
Impute SNPs based on population allele frequencies. This is used in
PCA tool currently.
"""

from typing import List, Dict, Optional
from loguru import logger
import numpy as np


class SNPsImputer:
    """Imputation of missing SNP data based on population allele frequencies.

    This tool is generally meant to be used internally by other ipa
    tools unless you know what you are doing.

    Parameters
    ----------
    data: np.ndarray
        A uint8 ndarray of (nsamples, nsnps) genotype calls that have
        already been filtered (usually by ipa.snps_extracter) to remove 
        any sites that are not wanted (e.g, non-biallelic). Only 
        0,1,2,9 should be in matrix, representing diploid genotypes 
        calls (0,1,2) or missing (9).
    names: List[str]
        Ordered list of names in same order as the rows of data.
    imap: Dict
        Dictionary mapping population names to lists of samples names.
        This is used to group samples for imputation so that allele
        frequencies can be sampled within population for each site.
    impute_method: str
        None, "sample", "simple", "kmeans"

    Example:
    ----------
    ...
    """
    def __init__(
        self,
        data: np.ndarray,
        names: List[str],
        imap: Dict[str,List[str]]=None,
        impute_method: str="sample",
        inplace: bool=False,
        random_seed: Optional[int]=None,
        ):

        # init attributes
        if not inplace:
            self.genos = data.copy()
        else:
            self.genos = data
        self.names = names
        self.imap = imap
        self.impute_method = impute_method
        self.rng = np.random.default_rng(random_seed)

    def run(self):
        """Impute data in-place filling missing in self.snps (9)."""
        if self.impute_method == "sample":
            self.genos = self._impute_sample()
        else:
            self.genos[self.genos == 9] = 0
            logger.info("Imputation: 'None'; (0, 1, 2) = 100%, 0%, 0%)")
        return self.genos

    def _impute_sample(self, imap: Optional[Dict[str,List[str]]]=None):
        """Sample derived alleles by their frequency in each population.

        Assigns genotype calls for all 9s in each column for each pop. 
        Uses the imap unless a different one is provided here (this
        option is used in kmeans pca where imap is updated iteratively).

        Generally the snps_extracter will be run with minmap=1, such 
        that every population has data for at least one individual,
        but this is not always the case, so a population could have 
        zero observations. In this case we cannot impute based on the
        frequencies in that population. And so in this rare case we 
        sample alleles from the total (pooled populations) frequencies.
        """
        # override imap
        imap = imap if imap is not None else self.imap

        # track imputed genotypes
        imputed_counts = {0: 0, 1: 0, 2: 0}

        # get pooled genotype frequencies
        total_obs = np.sum(self.genos != 9, axis=0) * 2
        total_marr = np.ma.array(self.genos, mask=self.genos==9)
        total_derived_freq = total_marr.sum(axis=0) / total_obs
        tot_sampled = self.rng.binomial(n=2, p=total_derived_freq, size=self.genos.shape)

        # impute data by mean value in each population
        for pop in imap:
            
            # get genos for just the samples in pop
            sidxs = sorted(self.names.index(i) for i in imap[pop])
            data = self.genos[sidxs, :]

            # number of alleles at each site that are not 9, X2 b/c we
            # want the max count of possible derived alleles.
            nobs = np.sum(data != 9, axis=0) * 2

            # sum of allele counts in sites that are not 9
            # (to get prob derived at each site)
            marr = np.ma.array(data, mask=data==9)
            fderived = marr.sum(axis=0) / nobs

            # pop sampler get two (0/1) samples for each diploid genotype
            pop_sampled = self.rng.binomial(n=2, p=fderived, size=data.shape)

            # mask for populations with no genotype calls
            colmask = marr.mask.all(axis=0)

            # fill unmasked columns with pop data and masked columns with tot data
            sampled = np.zeros(shape=data.shape, dtype=np.uint8)
            sampled[:, ~colmask] = pop_sampled[:, ~colmask]
            sampled[:, colmask] = tot_sampled[:, colmask][sidxs]

            # count imputed bases
            imputed = sampled[data==9]
            imputed_counts[0] += np.sum(imputed == 0)
            imputed_counts[1] += np.sum(imputed == 1)
            imputed_counts[2] += np.sum(imputed == 2)

            # fill to data copy and insert to genos
            data[data == 9] = imputed
            self.genos[sidxs, :] = data

        # get all imputed values
        total_imputed = sum(imputed_counts.values())
        if not total_imputed:
            logger.info("No missing data.")
        else:
            freq0 = 100 * imputed_counts[0] / total_imputed
            freq1 = 100 * imputed_counts[1] / total_imputed
            freq2 = 100 * imputed_counts[2] / total_imputed
            logger.info(
                "Imputation: sampled genotypes (0, 1, 2) = "
                f"{freq0:.1f}%, {freq1:.1f}%, {freq2:.1f}%"
            )
        # return genos for convenience.
        return self.genos


    # def _impute_sample_hier(self, imap=None):
    #     """DEPRECATED.
    #     Sample derived alleles by their frequency for each population and
    #     assign to fill 9 in each column for each pop. IF a population has
    #     no samples meeting the minmap requirement in the first round of
    #     imputation, then a second round is applied in which they sample
    #     a genotype based on the overall (non-IMAP) genotype frequencies.
    #     """
    #     if 1:
    #         raise NotImplementedError()

    #     # override imap
    #     if not imap:
    #         imap = self.imap

    #     # impute data by mean value in each population
    #     newdata = self.snps.copy()
    #     for pop, samps in imap.items():

    #         # sample pop data
    #         sidxs = sorted(self.names.index(i) for i in samps)
    #         data = newdata[sidxs, :].copy()

    #         # number of alleles at each site that are not 9
    #         nallels = np.sum(data != 9, axis=0) * 2

    #         # get prob derived at each site using tmp array w/ missing to zero
    #         tmp = data.copy()
    #         tmp[tmp == 9] = 0
    #         fderived = tmp.sum(axis=0) / nallels

    #         # sampler
    #         sampled = np.random.binomial(n=2, p=fderived, size=data.shape)
    #         data[data == 9] = sampled[data == 9]
    #         newdata[sidxs, :] = data

    #     # get all imputed values
    #     imputed = newdata[np.where(self.snps == 9)]
    #     logger.info(
    #         "Imputation: 'sampled'; (0, 1, 2) = {:.1f}%, {:.1f}%, {:.1f}%"
    #         .format(
    #             100 * np.sum(imputed == 0) / imputed.size,
    #             100 * np.sum(imputed == 1) / imputed.size,
    #             100 * np.sum(imputed == 2) / imputed.size,
    #         )
    #     )
    #     return newdata

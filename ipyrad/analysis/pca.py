#!/usr/bin/env python

""" Scikit-learn principal componenents analysis for missing data """

from __future__ import print_function, division

import os
import sys
import itertools
import numpy as np
import pandas as pd

# ipyrad tools
from .snps_extracter import SNPsExtracter
from .snps_imputer import SNPsImputer
from ipyrad.analysis.utils import jsubsample_snps
from .vcf_to_hdf5 import VCFtoHDF5 as vcf_to_hdf5
from ipyrad.assemble.utils import IPyradError

# missing imports to be raised on class init
try:
    import toyplot
except ImportError:
    pass

_MISSING_TOYPLOT = """
This ipyrad tool requires the plotting library toyplot. 
You can install it with the following command in a terminal.

conda install toyplot -c eaton-lab 
"""

try:
    from sklearn import decomposition 
    from sklearn.cluster import KMeans
    from sklearn.manifold import TSNE
    from sklearn.linear_model import LinearRegression
    from sklearn.neighbors import NearestCentroid   
except ImportError:
    pass

_MISSING_SKLEARN = """
This ipyrad tool requires the library scikit-learn.
You can install it with the following command in a terminal.

conda install scikit-learn -c conda-forge 
"""

_IMPORT_VCF_INFO = """
Converting vcf to HDF5 using default ld_block_size: {}
Typical RADSeq data generated by ipyrad/stacks will ignore this value.
You can use the ld_block_size parameter of the PCA() constructor to change
this value.
"""

# TODO: could allow LDA as alternative to PCA for supervised (labels) dsets.


class PCA(object):
    """
    Principal components analysis of RAD-seq SNPs with iterative
    imputation of missing data.

    Parameters:
    -----------
    data: (str, several options)
        A general .vcf file or a .snps.hdf5 file produced by ipyrad.
    workdir: (str; default="./analysis-pca")
        A directory for output files. Will be created if absent.
    imap: (dict; default=None)
        Dictionary mapping population names to a list of sample names.
    minmap: (dict; default={})
        Dictionary mapping population names to float values (X).
        If a site does not have data across X proportion of samples for
        each population, respectively, the site is filtered from the data set.
    mincov: (float; default=0.5)
        If a site does not have data across this proportion of total samples
        in the data then it is filtered from the data set.
    impute_method: (str; default='sample')
        None, "sample", or an integer for the number of kmeans clusters.
    topcov: (float; default=0.9)
        Affects kmeans method only.    
        The most stringent mincov used as the first iteration in kmeans 
        clustering. Subsequent iterations (niters) are equally spaced between
        topcov and mincov. 
    niters: (int; default=5)
        Affects kmeans method only.        
        Number of iterations of kmeans clustering with decreasing mincov 
        thresholds used to refine population clustering, and therefore to 
        refine the imap groupings used to filter and impute sites.
    ld_block_size: (int; default=20000)
        Only used during conversion of data imported as vcf.
        The size of linkage blocks (in base pairs) to split the vcf data into.

    Functions:
    ----------
    ...
    """
    def __init__(
        self, 
        data, 
        impute_method=None,
        imap=None,
        minmap=None,
        mincov=0.1,
        quiet=False,
        topcov=0.9,
        niters=5,
        ld_block_size=0,
        ):

        # only check import at init
        if not sys.modules.get("sklearn"):
            raise IPyradError(_MISSING_SKLEARN)
        if not sys.modules.get("toyplot"):
            raise IPyradError(_MISSING_TOYPLOT)

        # init attributes
        self.quiet = quiet
        self.data = os.path.realpath(os.path.expanduser(data))

        # data attributes
        self.impute_method = impute_method
        self.mincov = mincov        
        self.imap = (imap if imap else {})
        self.minmap = (minmap if minmap else {i: 1 for i in self.imap})
        self.topcov = topcov
        self.niters = niters
        self.ld_block_size = ld_block_size

        # where the resulting data are stored.
        self.pcaxes = None  # "No results, you must first call .run()"
        self.variances = None  # "No results, you must first call .run()"

        # to be filled
        self.snps = np.array([])
        self.snpsmap = np.array([])
        self.nmissing = 0

        # Works now. ld_block_size will have no effect on RAD data
        if self.data.endswith((".vcf", ".vcf.gz")):
            if not ld_block_size:
                self.ld_block_size = 20000
                if not self.quiet: 
                    print(_IMPORT_VCF_INFO.format(self.ld_block_size))

            converter = vcf_to_hdf5(
                name=data.split("/")[-1].split(".vcf")[0],
                data=self.data,
                ld_block_size=self.ld_block_size,
                quiet=quiet,
            )
            # run the converter
            converter.run()
            # Set data to the new hdf5 file
            self.data = converter.database

        # load .snps and .snpsmap from HDF5
        first = (True if isinstance(self.impute_method, int) else quiet)
        ext = SNPsExtracter(
            self.data, self.imap, self.minmap, self.mincov, quiet=first,
        )

        # run snp extracter to parse data files
        ext.parse_genos_from_hdf5()       
        self.snps = ext.snps
        self.snpsmap = ext.snpsmap
        self.names = ext.names
        self._mvals = ext._mvals

        # make imap for imputing if not used in filtering.
        if not self.imap:
            self.imap = {'1': self.names}
            self.minmap = {'1': 0.5}

        # record missing data per sample
        self.missing = pd.DataFrame({
            "missing": [0.],
            },
            index=self.names,
        )
        miss = np.sum(self.snps == 9, axis=1) / self.snps.shape[1]
        for name in self.names:
            self.missing.missing[name] = round(miss[self.names.index(name)], 2)

        # impute missing data
        if (self.impute_method is not False) and self._mvals:
            self._impute_data()


    def _seed(self):   
        return np.random.randint(0, 1e9)        


    def _print(self, msg):
        if not self.quiet:
            print(msg)


    def _impute_data(self):
        """
        Impute data in-place updating self.snps by filling missing (9) values.
        """
        # simple imputer method
        # if self.impute_method == "simple":
        # self.snps = SNPsImputer(
        # self.snps, self.names, self.imap, None).run()

        if self.impute_method == "sample":
            self.snps = SNPsImputer(
                self.snps, self.names, self.imap, "sample", self.quiet).run()

        elif isinstance(self.impute_method, int):
            self.snps = self._impute_kmeans(
                self.topcov, self.niters, self.quiet)

        else:
            self.snps[self.snps == 9] = 0
            self._print(
                "Imputation (null; sets to 0): {:.1f}%, {:.1f}%, {:.1f}%"
                .format(100, 0, 0)            
            )


    def _impute_kmeans(self, topcov=0.9, niters=5, quiet=False):

        # the ML models to fit
        pca_model = decomposition.PCA(n_components=None)  # self.ncomponents)
        kmeans_model = KMeans(n_clusters=self.impute_method)

        # start kmeans with a global imap
        kmeans_imap = {'global': self.names}

        # iterate over step values
        iters = np.linspace(topcov, self.mincov, niters)
        for it, kmeans_mincov in enumerate(iters):

            # start message
            kmeans_minmap = {i: self.mincov for i in kmeans_imap}
            self._print(
                "Kmeans clustering: iter={}, K={}, mincov={}, minmap={}"
                .format(it, self.impute_method, kmeans_mincov, kmeans_minmap))

            # 1. Load orig data and filter with imap, minmap, mincov=step
            se = SNPsExtracter(
                self.data, 
                imap=kmeans_imap, 
                minmap=kmeans_minmap, 
                mincov=kmeans_mincov,
                quiet=self.quiet,
            )
            se.parse_genos_from_hdf5()

            # update snpsmap to new filtered data to use for subsampling            
            self.snpsmap = se.snpsmap

            # 2. Impute missing data using current kmeans clusters
            impdata = SNPsImputer(
                se.snps, se.names, kmeans_imap, "sample", self.quiet).run()

            # x. On final iteration return this imputed array as the result
            if it == 4:
                return impdata

            # 3. subsample unlinked SNPs
            subdata = impdata[:, jsubsample_snps(se.snpsmap, self._seed())]

            # 4. PCA on new imputed data values
            pcadata = pca_model.fit_transform(subdata)

            # 5. Kmeans clustering to find new imap grouping
            kmeans_model.fit(pcadata)
            labels = np.unique(kmeans_model.labels_)           
            kmeans_imap = {
                i: [se.names[j] for j in 
                    np.where(kmeans_model.labels_ == i)[0]] for i in labels
            }
            self._print(kmeans_imap)
            self._print("")


    def _run(self, seed, subsample, quiet):
        """
        Called inside .run(). A single iteration. 
        """
        # sample one SNP per locus
        if subsample:
            data = self.snps[:, jsubsample_snps(self.snpsmap, seed)]
            if not quiet:
                print(
                    "Subsampling SNPs: {}/{}"
                    .format(data.shape[1], self.snps.shape[1])
                )
        else:
            data = self.snps

        # decompose pca call
        model = decomposition.PCA(None)  # self.ncomponents)
        model.fit(data)
        newdata = model.transform(data)
        variance = model.explained_variance_ratio_
        self._model = "PCA"

        # return tuple with new coordinates and variance explained
        return newdata, variance


    def run_and_plot_2D(self, ax0, ax1, seed=None, nreplicates=1, subsample=True, quiet=None):
        """
        Call .run() and .draw() in one single call. This is for simplicity. 
        In generaly you will probably want to call .run() and then .draw()
        as two separate calls. This way you can generate the results with .run()
        and then plot the stored results in many different ways using .draw().
        """
        # combine run and draw into one call for simplicity
        self.run(nreplicates=nreplicates, seed=seed, subsample=subsample, quiet=quiet)
        c, a, m = self.draw(ax0=ax0, ax1=ax1)
        return c, a, m


    def run(self, nreplicates=1, seed=None, subsample=True, quiet=None):
        """
        Decompose genotype array (.snps) into n_components axes. 

        Parameters:
        -----------
        nreplicates: (int)
            Number of replicate subsampled analyses to run. This is useful
            for exploring variation over replicate samples of unlinked SNPs.
            The .draw() function will show variation over replicates runs.
        seed: (int)
            Random number seed used if/when subsampling SNPs.
        subsample: (bool)
            Subsample one SNP per RAD locus to reduce effect of linkage.
        quiet: (bool)
            Print statements           

        Returns:
        --------      
        Two dctionaries are stored to the pca object in .pcaxes and .variances. 
        The first is the new data decomposed into principal coordinate space; 
        the second is an array with the variance explained by each PC axis. 
        """
        # default to 1 rep
        nreplicates = (nreplicates if nreplicates else 1)

        # option to override self.quiet for this run
        quiet = (quiet if quiet else self.quiet)

        # update seed. Numba seed cannot be None, so get random int if None
        seed = (seed if seed else self._seed())
        rng = np.random.RandomState(seed)

        # get data points for all replicate runs
        datas = {}
        vexps = {}
        datas[0], vexps[0] = self._run(
            subsample=subsample, 
            seed=rng.randint(0, 1e15), 
            quiet=quiet,
        )

        for idx in range(1, nreplicates):
            datas[idx], vexps[idx] = self._run(
                subsample=subsample, 
                seed=rng.randint(0, 1e15),
                quiet=True)

        # store results to object
        self.pcaxes = datas
        self.variances = vexps



    def draw(
        self, 
        ax0=0,
        ax1=1,
        cycle=8,
        colors=None,
        shapes=None,
        size=10,
        legend=True,
        imap=None,
        width=400, 
        height=300,
        axes=None,
        **kwargs):
        """
        Draw a scatterplot for data along two PC axes. 
        """
        self.drawing = Drawing(
            self, ax0, ax1, cycle, colors, shapes, size, legend,
            imap, width, height, axes,
            **kwargs)
        return self.drawing.canvas, self.drawing.axes  # , drawing.axes._children



    def draw_legend(self, axes, **kwargs):
        """
        Draw legend on a cartesian axes. This is intended to be added to a 
        custom setup canvas and axes configuration in toyplot. Example below:

        import toyplot
        canvas = toyplot.Canvas(width=1000, height=300)
        ax0 = canvas.cartesian(bounds=(50, 250, 50, 250))
        ax1 = canvas.cartesian(bounds=(350, 550, 50, 250))
        ax2 = canvas.cartesian(bounds=(650, 850, 50, 250))
        ax3 = canvas.cartesian(bounds=(875, 950, 50, 250))

        pca.draw(0, 1, axes=ax0, legend=False)
        pca.draw(0, 2, axes=ax1, legend=False)
        pca.draw(1, 3, axes=ax2, legend=False);
        pca.draw_legend(ax3, **{"font-size": "14px"})
        """

        # bail out if axes are not empty
        # if axes._children:
        #     print(
        #         "Warning: draw_legend() should be called on empty cartesian"
        #         " axes.\nSee the example in the docstring."
        #         )
        #     return

        # bail out if no drawing exists to add legend to.
        if not hasattr(self, "drawing"):
            print("You must first call .draw() to store a drawing.")
            return

        style = {
            "fill": "#262626", 
            "text-anchor": "start", 
            "-toyplot-anchor-shift": "15px",
            "font-size": "14px",
        }
        style.update(kwargs)


        skeys = sorted(self.drawing.imap)
        axes.scatterplot(
            np.repeat(0, len(self.drawing.imap)),
            np.arange(len(self.drawing.imap)),
            marker=[self.drawing.pstyles[i] for i in skeys],
        )
        axes.text(
            np.repeat(0, len(self.drawing.imap)),
            np.arange(len(self.drawing.imap)),
            [i for i in skeys],
            style=style,
        )
        axes.show = False



    def draw_panels(self, pc0=0, pc1=1, pc2=2, **kwargs):
        """
        A convenience function for drawing a three-part panel plot with the 
        first three PC axes. To do this yourself and further modify the layout
        you can start with the code below.

        Parameters (ints): three PC axes to plot.
        Returns: canvas

        ------------------------
        import toyplot
        canvas = toyplot.Canvas(width=1000, height=300)
        ax0 = canvas.cartesian(bounds=(50, 250, 50, 250))
        ax1 = canvas.cartesian(bounds=(350, 550, 50, 250))
        ax2 = canvas.cartesian(bounds=(650, 850, 50, 250))
        ax3 = canvas.cartesian(bounds=(875, 950, 50, 250))

        pca.draw(0, 1, axes=ax0, legend=False)
        pca.draw(0, 2, axes=ax1, legend=False)
        pca.draw(1, 3, axes=ax2, legend=False);
        pca.draw_legend(ax3, **{"font-size": "14px"})        
        """
        if self._model != "PCA":
            print("You must first call .run() to infer PC axes.")
            return

        canvas = toyplot.Canvas(width=1000, height=300)
        ax0 = canvas.cartesian(bounds=(50, 250, 50, 250))
        ax1 = canvas.cartesian(bounds=(350, 550, 50, 250))
        ax2 = canvas.cartesian(bounds=(650, 850, 50, 250))
        ax3 = canvas.cartesian(bounds=(875, 950, 50, 250))

        self.draw(pc0, pc1, axes=ax0, legend=False, **kwargs)
        self.draw(pc0, pc2, axes=ax1, legend=False, **kwargs)
        self.draw(pc1, pc2, axes=ax2, legend=False, **kwargs)
        self.draw_legend(ax3, **{"font-size": "14px"})
        return canvas



    def run_umap(self, subsample=True, seed=123, n_neighbors=15, **kwargs):
        """


        """
        # check just-in-time install
        try:
            import umap
        except ImportError:
            raise ImportError(
                "to use this function you must install umap with:\n"
                "  conda install umap-learn -c conda-forge "
                )

        # subsample SNPS
        seed = (seed if seed else self._seed())
        if subsample:
            data = self.snps[:, jsubsample_snps(self.snpsmap, seed)]
            print(
                "Subsampling SNPs: {}/{}"
                .format(data.shape[1], self.snps.shape[1])
            )
        else:
            data = self.snps

        # init TSNE model object with params (sensitive)
        umap_kwargs = {
            'n_neighbors': n_neighbors,
            'init': 'spectral', 
            'random_state': seed,
        }
        umap_kwargs.update(kwargs)
        umap_model = umap.UMAP(**umap_kwargs)

        # fit the model
        umap_data = umap_model.fit_transform(data)
        self.pcaxes = {0: umap_data}
        self.variances = {0: [-1.0, -2.0]}
        self._model = "UMAP"



    def run_tsne(self, subsample=True, perplexity=5.0, n_iter=1e6, seed=None, **kwargs):
        """
        Calls TSNE model from scikit-learn on the SNP or subsampled SNP data
        set. The 'seed' argument is used for subsampling SNPs. Perplexity
        is the primary parameter affecting the TSNE, but any additional 
        params supported by scikit-learn can be supplied as kwargs.
        """
        seed = (seed if seed else self._seed())
        if subsample:
            data = self.snps[:, jsubsample_snps(self.snpsmap, seed)]
            print(
                "Subsampling SNPs: {}/{}"
                .format(data.shape[1], self.snps.shape[1])
            )
        else:
            data = self.snps

        # init TSNE model object with params (sensitive)
        tsne_kwargs = {
            'perplexity': perplexity,
            'init': 'pca', 
            'n_iter': int(n_iter), 
            'random_state': seed,
        }
        tsne_kwargs.update(kwargs)
        tsne_model = TSNE(**tsne_kwargs)

        # fit the model
        tsne_data = tsne_model.fit_transform(data)
        self.pcaxes = {0: tsne_data}
        self.variances = {0: [-1.0, -2.0]}
        self._model = "TSNE"



    def pcs(self, rep=0):
        "return a dataframe with the PC loadings."
        try:
            df = pd.DataFrame(self.pcaxes[rep], index=self.names)
        except ValueError:
            raise IPyradError("You must call run() before accessing the pcs.")
        return df




class Drawing:
    def __init__(
        self,
        pcatool,
        ax0=0,
        ax1=1,
        cycle=8,
        colors=None,
        shapes=None,
        size=10,
        legend=True,
        imap=None,
        width=400, 
        height=300,
        axes=None,
        **kwargs):
        """
        See .draw() function above for docstring.
        """
        self.pcatool = pcatool
        self.datas = self.pcatool.pcaxes
        self.names = self.pcatool.names
        self.imap = (imap if imap else self.pcatool.imap)
        self.ax0 = ax0
        self.ax1 = ax1
        self.axes = axes

        # checks on user args
        self.cycle = cycle
        self.colors = colors
        self.shapes = shapes
        self.size = size
        self.legend = legend
        self.height = height
        self.width = width

        # parse attrs from the data
        self.nreplicates = None
        self.variance = None
        self._parse_replicate_runs()
        self._regress_replicates()

        # setup canvas and axes or use user supplied axes
        self.canvas = None
        self.axes = axes
        self._setup_canvas_and_axes()

        # add markers to the axes
        self.rstyles = {}
        self.pstyles = {}
        self._get_marker_styles()
        self._assign_styles_to_marks()
        self._draw_markers()

        # add the legend
        if self.legend and (self.canvas is not None):
            self._add_legend()



    def _setup_canvas_and_axes(self):
        # get axis labels for PCA or TSNE plot
        if self.variance[self.ax0] >= 0.0:
            xlab = "PC{} ({:.1f}%) explained".format(
                self.ax0, self.variance[self.ax0] * 100)
            ylab = "PC{} ({:.1f}%) explained".format(
                self.ax1, self.variance[self.ax1] * 100)
        else:
            xlab = "{} component 1".format(self.pcatool._model)
            ylab = "{} component 2".format(self.pcatool._model)


        if not self.axes:
            self.canvas = toyplot.Canvas(self.width, self.height)  # 400, 300)
            self.axes = self.canvas.cartesian(
                grid=(1, 5, 0, 1, 0, 4),  # <- leaves room for legend
                xlabel=xlab,
                ylabel=ylab,
            )
        else:
            self.axes.x.label.text = xlab
            self.axes.y.label.text = ylab



    def _parse_replicate_runs(self):

        # raise error if run() was not yet called.
        if self.datas is None:
            raise IPyradError(
                "You must first call run() before calling draw().")          

        try:
            # check for replicates in the data
            self.nreplicates = len(self.datas)
            self.variance = np.array(
                [i for i in self.pcatool.variances.values()]
            ).mean(axis=0)
        except AttributeError:
            raise IPyradError(
                "You must first call run() before calling draw().")

        # check that requested axes exist
        assert max(self.ax0, self.ax1) < self.datas[0].shape[1], (
            "data set only has {} axes.".format(self.datas[0].shape[1]))



    def _regress_replicates(self):
        """
        test reversions of replicate axes (clumpp like) so that all plot
        in the same orientation as replicate 0.
        """
        model = LinearRegression()
        for i in range(1, len(self.pcatool.pcaxes)):
            for ax in [self.ax0, self.ax1]:
                orig = self.datas[0][:, ax].reshape(-1, 1)
                new = self.datas[i][:, ax].reshape(-1, 1)
                swap = (self.datas[i][:, ax] * -1).reshape(-1, 1)

                # get r^2 for both model fits
                model.fit(orig, new)
                c0 = model.coef_[0][0]
                model.fit(orig, swap)
                c1 = model.coef_[0][0]

                # if swapped fit is better make this the data
                if c1 > c0:
                    self.datas[i][:, ax] = self.datas[i][:, ax] * -1



    def _get_marker_styles(self):
        # make reverse imap dictionary
        self.irev = {}
        for pop, vals in self.imap.items():
            for val in vals:
                self.irev[val] = pop

        # the max number of pops until color cycle repeats
        # If the passed in number of colors is big enough to cover
        # the number of pops then set cycle to len(colors)
        # If colors == None this first `if` falls through (lazy evaluation)
        if self.colors and len(self.colors) >= len(self.imap):
            self.cycle = len(self.colors)
        else:
            self.cycle = min(self.cycle, len(self.imap))

        # get color list repeating in cycles of cycle
        if not self.colors:
            self.colors = itertools.cycle(
                toyplot.color.broadcast(
                    toyplot.color.brewer.map("Spectral"), shape=self.cycle,
                )
            )
        else:
            self.colors = itertools.cycle(self.colors)
            # assert len(colors) == len(imap), "len colors must match len imap"

        # get shapes list repeating in cycles of cycle up to 5 * cycle
        if not self.shapes:
            self.shapes = itertools.cycle(np.concatenate([
                np.tile("o", self.cycle),
                np.tile("s", self.cycle),
                np.tile("^", self.cycle),
                np.tile("d", self.cycle),
                np.tile("v", self.cycle),
                np.tile("<", self.cycle),
                np.tile("x", self.cycle),            
            ]))
        else:
            self.shapes = itertools.cycle(self.shapes)
        # else:
            # assert len(shapes) == len(imap), "len colors must match len imap"            

        # assign styles to populations and to legend markers (no replicates)
        for idx, pop in enumerate(self.imap):

            icolor = next(self.colors)
            ishape = next(self.shapes)

            self.pstyles[pop] = toyplot.marker.create(
                size=self.size, 
                shape=ishape,
                mstyle={
                    "fill": toyplot.color.to_css(icolor),
                    "stroke": "#262626",
                    "stroke-width": 1.0,
                    "fill-opacity": 0.75,
                },
            )

            self.rstyles[pop] = toyplot.marker.create(
                size=self.size, 
                shape=ishape,
                mstyle={
                    "fill": toyplot.color.to_css(icolor),
                    "stroke": "none",
                    "fill-opacity": 0.9 / self.nreplicates,
                },
            )



    def _assign_styles_to_marks(self):
        # assign styled markers to data points
        self.pmarks = []
        self.rmarks = []
        for name in self.names:
            pop = self.irev[name]
            pmark = self.pstyles[pop]
            self.pmarks.append(pmark)
            rmark = self.rstyles[pop]
            self.rmarks.append(rmark)        



    def _draw_markers(self):

        # if not replicates then just plot the points
        if self.nreplicates < 2:
            mark = self.axes.scatterplot(
                self.datas[0][:, self.ax0],
                self.datas[0][:, self.ax1],
                marker=self.pmarks,
                title=self.names,
            )

        else:
            # add the replicates cloud points       
            for i in range(self.nreplicates):
                # get transformed coordinates and variances
                mark = self.axes.scatterplot(
                    self.datas[i][:, self.ax0],
                    self.datas[i][:, self.ax1],
                    marker=self.rmarks,
                )

            # compute centroids
            Xarr = np.concatenate([
                np.array(
                    [self.datas[i][:, self.ax0], self.datas[i][:, self.ax1]]).T 
                for i in range(self.nreplicates)
            ])
            yarr = np.tile(np.arange(len(self.names)), self.nreplicates)
            clf = NearestCentroid()
            clf.fit(Xarr, yarr)

            # draw centroids
            mark = self.axes.scatterplot(
                clf.centroids_[:, 0],
                clf.centroids_[:, 1],
                title=self.names,
                marker=self.pmarks,
            )



    def _add_legend(self, corner=None):
        """
        Default arg:
        corner = ("right", 35, 100, min(250, len(self.pstyles) * 25))
        """
        if corner is None:
            corner = ("right", 35, 100, min(250, len(self.pstyles) * 25))

        # add a legend
        if len(self.imap) > 1:
            marks = [(pop, marker) for pop, marker in self.pstyles.items()]
            self.canvas.legend(
                marks, 
                corner=corner,
            )

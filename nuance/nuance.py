import jax
from tinygp import kernels, GaussianProcess
import numpy as np
import jax.numpy as jnp
from . import utils
from tqdm.autonotebook import tqdm
from .search_data import SearchData
import jaxopt
import numpy as np
from dataclasses import dataclass
import multiprocessing as mp
from functools import partial


@dataclass
class Nuance:
    def __init__(
        self, time, flux, error=None, kernel=None, X=None, compute=True, mean=0.0
    ):
        """Nuance

        Parameters
        ----------
        x : array
            dimension
        y : array
            observed
        gp : array or tinygp.gp.GaussianProcess
            error or tinygp.GaussianProcess instance
        X : ndarray, optional
            design matrix, by default None
        """
        self.time = time
        self.flux = flux
        self.error = error
        self.kernel = kernel
        self.mean = mean

        if X is None:
            X = np.atleast_2d(np.ones_like(time))

        self.X = X

        if kernel is None:
            kernel = kernels.Constant(0.0)

        self.gp = GaussianProcess(kernel, time, diag=error**2, mean=mean)

        if compute:
            self._compute_L()

        self.search_data = None

    def _compute_L(self):

        Liy = self.gp.solver.solve_triangular(self.flux)
        LiX = self.gp.solver.solve_triangular(self.X.T)

        @jax.jit
        def eval_m(m):
            Xm = jnp.vstack([self.X, m])
            Lim = self.gp.solver.solve_triangular(m)
            LiXm = jnp.hstack([LiX, Lim[:, None]])
            LiXmT = LiXm.T
            LimX2 = LiXmT @ LiXm
            w = jnp.linalg.lstsq(LimX2, LiXmT @ Liy)[0]
            v = jnp.linalg.inv(LimX2)
            return self.gp.log_probability(self.flux - w @ Xm), w, v

        self.eval_m = eval_m

    @property
    def ll0(self) -> float:
        """log-likelihood of data to a model without transit

        Returns
        -------
        float
        """
        return self.eval_m(np.zeros_like(self.time))[0].__array__()

    def linear_search(
        self,
        t0s: np.ndarray,
        Ds: np.ndarray,
        positive: bool = True,
        progress: bool = True,
    ):
        """Performs the linear search. Saves the linear search `Nuance.search_data` as a :py:class:`nuance.SearchData` object

        Parameters
        ----------
        t0s : np.ndarray
            array of transit epochs
        Ds : np.ndarray
            array of transit durations
        positive : bool, optional
            wether to force depth to be positive, by default True
        progress : bool, optional
            wether to show progress bar, by default True

        Returns
        -------
        None
        """

        ll = np.zeros((len(t0s), len(Ds)))
        depths = np.zeros((len(t0s), len(Ds)))
        vars = ll.copy()
        depths = ll.copy()
        n = len(self.X)

        _progress = lambda x: tqdm(x) if progress else x

        @jax.jit
        def eval_transit(t0, D):
            m = utils.single_transit(self.time, t0, D)
            _ll, w, v = self.eval_m(m)
            return w[n], v[n, n], _ll

        f = jax.vmap(eval_transit, in_axes=(None, 0))
        for i, t0 in enumerate(_progress(t0s)):
            depths[i, :], vars[i, :], ll[i, :] = f(t0, Ds)

        ll = np.array(ll)

        if positive:
            ll0 = self.eval_m(np.zeros_like(self.time))[0]
            ll[depths < 0] = ll0

        vars[~np.isfinite(vars)] = 1e25

        self.search_data = SearchData(
            t0s=t0s, Ds=Ds, ll=ll, z=depths, vz=vars, ll0=self.ll0
        )

    def periodic_search(self, periods: np.ndarray, progress=True):
        """Performs the periodic search

        Parameters
        ----------
        periods : np.ndarray
            array of periods to search
        progress : bool, optional
            wether to show progress bar, by default True

        Returns
        -------
        :py:class:`nuance.SearchData`
            search results
        """
        new_search_data = self.search_data.copy()
        fold_ll = new_search_data.fold_ll
        n = len(periods)
        snr = np.zeros(n)
        max_ll = snr.copy()
        params = np.zeros((n, 3))

        _progress = lambda x: tqdm(x) if progress else x

        for p, P in enumerate(_progress(periods)):
            phase, P1, P2 = fold_ll(P)
            i, j = np.unravel_index(np.argmax(P2), P2.shape)
            Ti = phase[i] * P
            Dj = new_search_data.Ds[j]
            snr[p], params[p] = float(self.snr(Ti, Dj, P)), (Ti, Dj, P)
            max_ll[p] = P2[i, j] - P2.mean()

        new_search_data.periods = periods
        new_search_data.Q_snr = snr
        new_search_data.Q_ll = max_ll
        new_search_data.Q_params = params

        return new_search_data

    def _models(self, m):
        _, w, _ = self.eval_m(m)
        mean = w[0:-1] @ self.X
        signal = m * w[-1]
        _, cond = self.gp.condition(self.flux - mean - signal)
        noise = cond.mean

        return mean, signal, noise

    def mu(self, mask=None):
        if mask is None:
            mask = mask = np.ones_like(self.time).astype(bool)

        masked_x = self.time[mask]
        masked_y = self.flux[mask]
        masked_X = self.X[:, mask]

        @jax.jit
        def _mu():
            gp = self.gp
            _, w, _ = self.eval_m(np.zeros_like(self.time))
            w = w[0:-1]
            cond_gp = gp.condition(masked_y - w @ masked_X, self.time).gp
            return cond_gp.loc + w @ self.X

        return _mu()

    def models(self, t0: float, D: float, P: float=None):
        """Return the models corresponding the transit of epoch `t0` and duration `D`(and period `P` for a periodic transit)

        Parameters
        ----------
        t0 : float
            epoch, same unit as time
        D : float
            duration, same unit as time
        P : float, optional
            period, same unit as time, by default None

        Returns
        -------
        list np.ndarray
            a list of three np.ndarray:

            - linear: linear model
            - astro: signal being searched (transit)
            - noise: noise model

        Example
        -------

        .. code-block::

            from nuance import Nuance, utils

            time, flux, error = utils.simulation()[0]

            nu = Nuance(time, flux, error)
            linear, astro, noise = nu.models(0.2, 0.05, 1.3)

        """
        m = utils.transit(self.time, t0, D, 1, P=P)
        return self._models(m)

    def solve(self, t0:float, D:float, P:float=None):
        """solve linear model (design matrix `Nuance.X`)

        Parameters
        ----------
        t0 : float
            epoch, same unit as time
        D : float
            duration, same unit as time
        P : float, optional
            period, same unit as time, by default None

        Returns
        -------
        list
            (w, v): linear coefficients and their covariance matrix
        """
        m = utils.transit(self.time, t0, D, 1, P=P)
        _, w, v = self.eval_m(m)
        return w, v

    def depth(self, t0: float, D: float, P: float=None):
        """depth linearly solved for epoch `t0` and duration `D` (and period `P` for a periodic transit)

        Parameters
        ----------
        t0 : float
            epoch, same unit as time
        D : float
            duration, same unit as time
        P : float, optional
            period, same unit as time, by default None

        Returns
        -------
        float, float
            transit depth, depth error
        """
        w, v = self.solve(t0, D, P)
        return w[-1], np.sqrt(v[-1, -1])

    def snr(self, t0: float, D: float, P: float=None):
        """SNR of transit linearly solved for epoch `t0` and duration `D` (and period `P` for a periodic transit)

        Parameters
        ----------
        t0 : float
            epoch, same unit as time
        D : float
            duration, same unit as time
        P : float, optional
            period, same unit as time, by default None

        Returns
        -------
        float
            transit snr
        """
        w, dw = self.depth(t0, D, P=P)
        return w / dw

    def gp_optimization(self, build_gp, mask=None):
        if mask is None:
            mask = mask = np.ones_like(self.time).astype(bool)

        masked_x = self.time[mask]
        masked_y = self.flux[mask]
        masked_X = self.X[:, mask]

        @jax.jit
        def nll_w(params):
            gp = build_gp(params, masked_x)
            Liy = gp.solver.solve_triangular(masked_y)
            LiX = gp.solver.solve_triangular(masked_X.T)
            LiXT = LiX.T
            LiX2 = LiXT @ LiX
            w = jnp.linalg.lstsq(LiX2, LiXT @ Liy)[0]
            nll = -gp.log_probability(masked_y - w @ masked_X)
            return nll, w

        @jax.jit
        def nll(params):
            return nll_w(params)[0]

        @jax.jit
        def mu(params):
            gp = build_gp(params, masked_x)
            _, w = nll_w(params)
            cond_gp = gp.condition(masked_y - w @ masked_X, self.time).gp
            return cond_gp.loc + w @ self.X

        def optimize(init_params, param_names=None):
            def inner(theta, *args, **kwargs):
                params = dict(init_params, **theta)
                return nll(params, *args, **kwargs)

            param_names = (
                list(init_params.keys()) if param_names is None else param_names
            )
            start = {k: init_params[k] for k in param_names}

            solver = jaxopt.ScipyMinimize(fun=inner)
            soln = solver.run(start)
            print(soln.state)

            return dict(init_params, **soln.params)

        return optimize, mu, nll

    def mask(self, t0, D, P):
        """Return a `Nuance` where the transit of epoch `t0` and duration `D` and period `P` is masked.

        Parameters
        ----------
        t0 : _type_
            _description_
        D : _type_
            _description_
        P : _type_
            _description_

        Returns
        -------
        _type_
            _description_
        """
        # search data
        search_data = self.search_data.copy()
        ph = utils.phase(search_data.t0s, t0, P)
        mask = np.abs(ph) > 2 * D

        search_data.llv = None
        search_data.llc = None
        search_data.periods = None
        search_data.t0s = search_data.t0s[mask]
        search_data.ll = search_data.ll[mask]
        search_data.z = search_data.z[mask]
        search_data.vz = search_data.vz[mask]

        # nu
        ph = utils.phase(self.time, t0, P)
        mask = np.abs(ph) > 2 * D
        if isinstance(self.error, np.ndarray):
            error = self.error[mask]
        else:
            error = self.error

        nu = Nuance(
            self.time[mask],
            self.flux[mask],
            error=error,
            kernel=self.kernel,
            X=self.X[:, mask],
        )
        nu.search_data = search_data
        return nu

#  Authors: Artem Pulkin, pyscf authors
"""
This module implements the G0W0 approximation on top of `pyscf.tdscf.rhf_slow` TDHF implementation. Unlike `gw.py`, all
integrals are stored in memory. Several variants of GW are available:

 * (this module) `pyscf.gw_slow`: the molecular implementation;
 * `pyscf.pbc.gw.gw_slow`: single-kpoint PBC (periodic boundary condition) implementation;
 * `pyscf.pbc.gw.kgw_slow_supercell`: a supercell approach to PBC implementation with multiple k-points. Runs the
   molecular code for a model with several k-points for the cost of discarding momentum conservation and using dense
   instead of sparse matrixes;
 * `pyscf.pbc.gw.kgw_slow`: a PBC implementation with multiple k-points;
"""

from pyscf.lib import einsum, direct_sum
from pyscf.lib import logger, temporary_env

import numpy
from scipy.optimize import newton, bisect

from itertools import product

# Convention for these modules:
# * IMDS contains routines for intermediates
# * kernel finds GW roots
# * GW provides a container


class AbstractIMDS(object):
    orb_dims = 1

    def __init__(self, tdhf):
        """
        GW intermediates.
        Args:
            tdhf (TDRHF): the TDRHF contatainer;
        """
        self.eri = tdhf.eri
        self.tdhf = tdhf
        self.mf = tdhf._scf

    def get_rhs(self, p, components=False):
        """
        The right-hand side of the quasiparticle equation.
        Args:
            p (int, tuple): the orbital;
            components (bool): if True, returns separate components which have to be summed to obtain the rhs;

        Returns:
            Right-hand sides of the quasiparticle equation
        """
        raise NotImplementedError

    def entire_space(self):
        """
        The entire orbital space.
        Returns:
            An iterable of the entire orbital space.
        """
        raise NotImplementedError

    def get_sigma_element(self, omega, p, **kwargs):
        """
        The diagonal matrix element of the self-energy matrix.
        Args:
            omega (float): the energy value;
            p (int, tuple): the orbital;

        Returns:
            The diagonal matrix element.
        """
        raise NotImplementedError

    def quasiparticle_eq(self, p, **kwargs):
        """
        The quasiparticle equation `f(omega) = 0`.
        Args:
            p (int, tuple): the orbital;
            **kwargs: keyword arguments to `get_sigma_element`;

        Returns:
            A callable function of one parameter.
        """
        rhs = self.get_rhs(p)

        def quasiparticle_eq(omega):
            return omega - self.get_sigma_element(omega, p, **kwargs).real - rhs

        return quasiparticle_eq

    def initial_guess(self, p):
        """
        Retrieves the initial guess for the quasiparticle energy for orbital `p`.
        Args:
            p (int, tuple): the orbital;

        Returns:
            The value of initial guess (float).
        """
        raise NotImplementedError


class IMDS(AbstractIMDS):
    def __init__(self, tdhf):
        """
        GW intermediates.
        Args:
            tdhf (TDRHF): the TDRHF contatainer;
        """
        super(IMDS, self).__init__(tdhf)

        # MF
        self.nocc = self.eri.nocc
        self.o, self.v = self.eri.mo_energy[:self.nocc], self.eri.mo_energy[self.nocc:]
        backup = self.mf.mo_occ.copy()
        self.mf.mo_occ[~self.eri.space] = 0
        if "exxdiv" in dir(self.mf):
            with temporary_env(self.mf, exxdiv=None):
                self.v_mf = self.mf.get_veff() - self.mf.get_j()
        else:
            self.v_mf = self.mf.get_veff() - self.mf.get_j()
        self.mf.mo_occ = backup

        # TD
        self.td_xy = self.tdhf.xy
        self.td_e = self.tdhf.e

        self.tdm = self.construct_tdm()

    def __getitem__(self, item):
        return self.eri[item]

    def get_rhs(self, p, components=False):
        # 1
        moe = self.eri.mo_energy[p]
        # 2
        if p < self.nocc:
            vk = - numpy.trace(self["oooo"][p, :, :, p])
        else:
            vk = - numpy.trace(self["ovvo"][:, p-self.nocc, p-self.nocc, :])
        # 3
        v_mf = einsum("i,ij,j", self.eri.mo_coeff[:, p].conj(), self.v_mf, self.eri.mo_coeff[:, p])
        if components:
            return moe, vk, -v_mf
        else:
            return moe + vk - v_mf

    def construct_tdm(self):
        td_xy = 2 * numpy.asarray(self.td_xy)
        tdm_oo = einsum('vxia,ipaq->vxpq', td_xy, self["oovo"])
        tdm_ov = einsum('vxia,ipaq->vxpq', td_xy, self["oovv"])
        tdm_vv = einsum('vxia,ipaq->vxpq', td_xy, self["ovvv"])

        if numpy.iscomplexobj(self["oovv"]):
            tdm_vo = einsum('vxia,ipaq->vxpq', td_xy, self["ovvo"])
        else:
            tdm_vo = tdm_ov.swapaxes(2, 3).conj()

        tdm = numpy.concatenate(
            (
                numpy.concatenate((tdm_oo, tdm_ov), axis=3),
                numpy.concatenate((tdm_vo, tdm_vv), axis=3)
            ),
            axis=2,
        )
        return tdm

    def get_sigma_element(self, omega, p, eta, vir_sgn=1):
        tdm = self.tdm.sum(axis=1)
        evi = direct_sum('v-i->vi', self.td_e, self.o)
        eva = direct_sum('v+a->va', self.td_e, self.v)
        sigma = numpy.sum(tdm[:, :self.nocc, p] ** 2 / (omega + evi - 1j * eta))
        sigma += numpy.sum(tdm[:, self.nocc:, p] ** 2 / (omega - eva + vir_sgn * 1j * eta))
        return sigma

    def initial_guess(self, p):
        return self.eri.mo_energy[p]

    @property
    def entire_space(self):
        return [numpy.arange(self.eri.nmo)]


class LoggingFunction(object):
    def __init__(self, m):
        """
        A function number->number logging calls.
        Args:
            m (callable): an underlying method of a single number returning a number;
        """
        self.m = m
        self.__x__ = []
        self.__y__ = []

    @property
    def x(self):
        return numpy.asarray(self.__x__)

    @property
    def y(self):
        return numpy.asarray(self.__y__)

    def __call__(self, x):
        y = self.m(x)
        self.__x__.append(x)
        self.__y__.append(y)
        return y

    def plot_call_history(self, title=""):
        """
        Plots calls to this function.
        Args:
            title (str): plot title;
        """
        if len(self.x) > 1:
            from matplotlib import pyplot
            x = self.x.real
            y = self.y.real
            pyplot.scatter(x[1:], y[1:], marker='+', color="black", s=10)
            pyplot.scatter(x[:1], y[:1], marker='+', color="red", s=50)
            pyplot.axhline(y=0, color="grey")
            pyplot.title(title + " ncalls: {:d}".format(len(self.x)))
            pyplot.show()


def kernel(imds, orbs=None, linearized=False, eta=1e-3, tol=1e-9, method="fallback"):
    """
    Calculates GW energies.
    Args:
        imds (AbstractIMDS): GW intermediates;
        orbs (Iterable): indexes of MO orbitals to correct;
        linearized (bool): whether to apply a single-step linearized correction to energies instead of iterative
        procedure;
        eta (float): imaginary energy for the Green's function;
        tol (float): tolerance for the search of zero;
        method (str): 'bisect' finds roots no matter what but, potentially, wrong ones, 'newton' finding roots close to
        the correct one but, potentially, failing during iterations, or 'fallback' using 'newton' and proceeding to
        'bisect' in case of failure;

    Returns:
        Corrected orbital energies.
    """
    if method not in ('newton', 'bisect', 'fallback'):
        raise ValueError("Cannot recognize method='{}'".format(method))

    # Check implementation consistency
    _orbs = imds.entire_space
    if not isinstance(_orbs, list) or not len(_orbs) == imds.orb_dims:
        raise RuntimeError("The object returned by 'imds.entire_space' is not a list of length {:d}: {}".format(
            imds.orb_dims,
            repr(_orbs),
        ))

    # Assign default value
    if orbs is None:
        orbs = _orbs

    # Make sure it is a list
    if not isinstance(orbs, list):
        orbs = [orbs]

    # Add missing dimensions
    if len(orbs) < imds.orb_dims:
        orbs = _orbs[:-len(orbs)] + orbs

    shape = tuple(len(i) for i in orbs)
    gw_energies = numpy.zeros(shape, dtype=float)

    for i_p in product(*tuple(numpy.arange(i) for i in shape)):
        p = tuple(i[j] for i, j in zip(orbs, i_p))
        if imds.orb_dims == 1:
            p = p[0]
        if linearized:
            raise NotImplementedError
            # v_mf = imds.vmf
            # vk = imds.vk
            # de = 1e-6
            # ep = imds.e_mf[p]
            # # TODO: analytic sigma derivative
            # sigma = imds.get_sigma_element(ep, p, eta).real
            # dsigma = imds.get_sigma_element(ep + de, p, eta).real - sigma
            # zn = 1.0 / (1 - dsigma / de)
            # e = ep + zn * (sigma.real + vk[p] - v_mf[p])
            # gw_energies[i_p] = e
        else:
            debug = LoggingFunction(imds.quasiparticle_eq(p, eta=eta))

            if method == "newton":
                try:
                    gw_energies[i_p] = newton(debug, imds.initial_guess(p), tol=tol, maxiter=100)
                except Exception as e:
                    e.message = "When calculating root @p={} the following exception occurred:\n\n{}".format(
                        repr(p),
                        e.message,
                    )
                    debug.plot_call_history("Exception during Newton " + str(p))
                    raise

            elif method == "bisect":
                gw_energies[i_p] = bisect(debug, -100, 100, xtol=tol, maxiter=100)

            elif method == "fallback":
                try:
                    gw_energies[i_p] = newton(debug, imds.initial_guess(p), tol=tol, maxiter=100)
                except RuntimeError:
                    logger.warn(imds.mf, "Failed to converge with newton, using bisect on the interval [{:.3e}, {:.3e}]".format(
                        min(debug.x), max(debug.x),
                    ))
                    gw_energies[i_p] = bisect(debug, min(debug.x), max(debug.x), xtol=tol, maxiter=100)

    return gw_energies


class GW(object):
    base_imds = IMDS

    def __init__(self, tdhf):
        """
        Performs GW calculation. Roots are stored in `self.mo_energy`.
        Args:
            tdhf (TDRHF): the base time-dependent restricted Hartree-Fock model;
        """
        self.tdhf = tdhf
        self.imds = self.base_imds(tdhf)
        self.mo_energy = None
        self.orbs = None
        self.method = "fallback"
        self.eta = 1e-3

    def kernel(self):
        """
        Calculates GW roots.

        Returns:
            GW roots.
        """
        self.mo_energy = kernel(self.imds, orbs=self.orbs, method=self.method, eta=self.eta)
        return self.mo_energy

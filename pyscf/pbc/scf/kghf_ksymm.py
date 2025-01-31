#!/usr/bin/env python
# Copyright 2014-2020 The PySCF Developers. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Xing Zhang <zhangxing.nju@gmail.com>
#

import numpy as np
import scipy
from pyscf import __config__
from pyscf import lib
from pyscf.lib import logger
from pyscf.pbc.lib import kpts as libkpts
from pyscf.pbc.scf import kghf
from pyscf.pbc.scf import khf_ksymm
from pyscf.pbc.df.df_jk import _format_jks

def get_jk(mf, cell=None, dm_kpts=None, hermi=0, kpts=None, kpts_band=None,
           with_j=True, with_k=True, **kwargs):
    if isinstance(kpts, np.ndarray):
        return kghf.get_jk(mf, cell, dm_kpts, hermi, kpts, kpts_band,
                           with_j, with_k, **kwargs)

    if cell is None: cell = mf.cell
    if dm_kpts is None: dm_kpts = mf.make_rdm1()
    if kpts is None: kpts = mf.kpts
    if kpts_band is None: kpts_band = kpts.kpts_ibz
    nkpts = kpts.nkpts
    nkpts_ibz = kpts.nkpts_ibz
    nband = len(kpts_band)

    dm_kpts = np.asarray(dm_kpts)
    nso = dm_kpts.shape[-1]
    nao = nso // 2
    dms = dm_kpts.reshape(-1,nkpts_ibz,nso,nso)
    n_dm = dms.shape[0]

    dmaa = np.empty([n_dm, nkpts, nao, nao], dtype=np.complex128)
    dmab = np.empty([n_dm, nkpts, nao, nao], dtype=np.complex128)
    dmbb = np.empty([n_dm, nkpts, nao, nao], dtype=np.complex128)
    for i in range(n_dm):
        dmaa[i] = kpts.transform_dm(dms[i,:,:nao,:nao])
        dmab[i] = kpts.transform_dm(dms[i,:,nao:,:nao])
        dmbb[i] = kpts.transform_dm(dms[i,:,nao:,nao:])
    dms = np.vstack((dmaa, dmbb, dmab))

    j1, k1 = mf.with_df.get_jk(dms, hermi, kpts.kpts, kpts_band, with_j, with_k,
                               exxdiv=mf.exxdiv)
    j1 = j1.reshape(3,n_dm,nband,nao,nao)
    k1 = k1.reshape(3,n_dm,nband,nao,nao)

    vj = vk = None
    if with_j:
        vj = np.zeros((n_dm,nband,nso,nso), j1.dtype)
        vj[:,:,:nao,:nao] = vj[:,:,nao:,nao:] = j1[0] + j1[1]
        vj = _format_jks(vj, dm_kpts, kpts_band, kpts.kpts_ibz)

    if with_k:
        vk = np.zeros((n_dm,nband,nso,nso), k1.dtype)
        vk[:,:,:nao,:nao] = k1[0]
        vk[:,:,nao:,nao:] = k1[1]
        vk[:,:,:nao,nao:] = k1[2]
        vk[:,:,nao:,:nao] = k1[2].transpose(0,1,3,2).conj()
        vk = _format_jks(vk, dm_kpts, kpts_band, kpts.kpts_ibz)

    return vj, vk

def get_occ(mf, mo_energy_kpts=None, mo_coeff_kpts=None):
    '''Label the occupancies for each orbital for sampled k-points.

    This is a k-point version of scf.hf.SCF.get_occ
    '''
    if mo_energy_kpts is None: mo_energy_kpts = mf.mo_energy
    kpts = mf.kpts
    mo_energy_kpts = kpts.transform_mo_energy(mo_energy_kpts)

    nkpts = len(mo_energy_kpts)
    nocc = mf.cell.nelectron * nkpts

    mo_energy = np.sort(np.hstack(mo_energy_kpts))
    fermi = mo_energy[nocc-1]
    mo_occ_kpts = []
    for mo_e in mo_energy_kpts:
        mo_occ_kpts.append((mo_e <= fermi).astype(np.double))

    if nocc < mo_energy.size:
        logger.info(mf, 'HOMO = %.12g  LUMO = %.12g',
                    mo_energy[nocc-1], mo_energy[nocc])
        if mo_energy[nocc-1]+1e-3 > mo_energy[nocc]:
            logger.warn(mf, 'HOMO %.12g == LUMO %.12g',
                        mo_energy[nocc-1], mo_energy[nocc])
    else:
        logger.info(mf, 'HOMO = %.12g', mo_energy[nocc-1])

    if mf.verbose >= logger.DEBUG:
        np.set_printoptions(threshold=len(mo_energy))
        logger.debug(mf, '     k-point                  mo_energy')
        for k,kpt in enumerate(mf.cell.get_scaled_kpts(mf.kpts, kpts_in_ibz=False)):
            logger.debug(mf, '  %2d (%6.3f %6.3f %6.3f)   %s %s',
                         k, kpt[0], kpt[1], kpt[2],
                         mo_energy_kpts[k][mo_occ_kpts[k]> 0],
                         mo_energy_kpts[k][mo_occ_kpts[k]==0])
        np.set_printoptions(threshold=1000)

    mo_occ_kpts = kpts.check_mo_occ_symmetry(mo_occ_kpts)
    return mo_occ_kpts


class KsymAdaptedKGHF(khf_ksymm.KsymAdaptedKSCF, kghf.KGHF):
    """
    KGHF with k-point symmetry
    """
    def __init__(self, cell, kpts=libkpts.KPoints(),
                 exxdiv=getattr(__config__, 'pbc_scf_SCF_exxdiv', 'ewald')):
        self._kpts = None
        kghf.KGHF.__init__(self, cell, kpts, exxdiv)

    def get_hcore(self, cell=None, kpts=None):
        hcore = khf_ksymm.KsymAdaptedKSCF.get_hcore(self, cell, kpts)
        return lib.asarray([scipy.linalg.block_diag(h, h) for h in hcore])

    def get_ovlp(self, cell=None, kpts=None):
        s = khf_ksymm.KsymAdaptedKSCF.get_ovlp(self, cell, kpts)
        return lib.asarray([scipy.linalg.block_diag(x, x) for x in s])

    get_jk = get_jk
    get_occ = get_occ
    energy_elec = khf_ksymm.KsymAdaptedKSCF.energy_elec
    get_init_guess = khf_ksymm.KsymAdaptedKSCF.get_init_guess

KGHF = KsymAdaptedKGHF

if __name__ == "__main__":
    from pyscf.pbc import gto, scf
    cell = gto.Cell()
    cell.atom = '''
        H 0 0 0
        H 1 0 0
        H 0 1 0
        H 0 0 1
    '''
    cell.a = np.eye(3)*2
    cell.basis = [[0, [1.2, 1]]]
    cell.verbose = 5
    cell.build()

    kpts = cell.make_kpts([2,2,1],space_group_symmetry=True,time_reversal_symmetry=True)
    mf = scf.KGHF(cell, kpts)
    mf.kernel()

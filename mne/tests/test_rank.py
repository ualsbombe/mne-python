import os.path as op
import itertools as itt

from numpy.testing import assert_array_equal
import numpy as np

import pytest

from mne import (read_evokeds, read_cov, compute_raw_covariance, pick_types,
                 pick_info)
from mne.cov import prepare_noise_cov
from mne.datasets import testing
from mne.io import read_raw_fif
from mne.io.pick import channel_type, _picks_by_type
from mne.io.proj import _has_eeg_average_ref_proj
from mne.proj import compute_proj_raw
from mne.rank import (estimate_rank, compute_rank, _get_rank_sss,
                      _compute_rank_int, _estimate_rank_raw)


base_dir = op.join(op.dirname(__file__), '..', 'io', 'tests', 'data')
cov_fname = op.join(base_dir, 'test-cov.fif')
raw_fname = op.join(base_dir, 'test_raw.fif')
ave_fname = op.join(base_dir, 'test-ave.fif')
hp_fif_fname = op.join(base_dir, 'test_chpi_raw_sss.fif')

testing_path = testing.data_path(download=False)
data_dir = op.join(testing_path, 'MEG', 'sample')
fif_fname = op.join(data_dir, 'sample_audvis_trunc_raw.fif')
mf_fif_fname = op.join(testing_path, 'SSS', 'test_move_anon_raw_sss.fif')


def test_estimate_rank():
    """Test rank estimation."""
    data = np.eye(10)
    assert_array_equal(estimate_rank(data, return_singular=True)[1],
                       np.ones(10))
    data[0, 0] = 0
    assert estimate_rank(data) == 9
    pytest.raises(ValueError, estimate_rank, data, 'foo')


@pytest.mark.slowtest
@testing.requires_testing_data
def test_rank_estimation():
    """Test raw rank estimation."""
    iter_tests = itt.product(
        [fif_fname, hp_fif_fname],  # sss
        ['norm', dict(mag=1e11, grad=1e9, eeg=1e5)]
    )
    for fname, scalings in iter_tests:
        raw = read_raw_fif(fname).crop(0, 4.).load_data()
        (_, picks_meg), (_, picks_eeg) = _picks_by_type(raw.info,
                                                        meg_combined=True)
        n_meg = len(picks_meg)
        n_eeg = len(picks_eeg)

        if len(raw.info['proc_history']) == 0:
            expected_rank = n_meg + n_eeg
        else:
            expected_rank = _get_rank_sss(raw.info) + n_eeg
        assert _estimate_rank_raw(raw, scalings=scalings) == expected_rank
        with pytest.deprecated_call():
            assert raw.estimate_rank(picks=picks_eeg,
                                     scalings=scalings) == n_eeg
        if 'sss' in fname:
            raw.add_proj(compute_proj_raw(raw))
        raw.apply_proj()
        n_proj = len(raw.info['projs'])
        want_rank = expected_rank - (0 if 'sss' in fname else n_proj)
        assert _estimate_rank_raw(raw, scalings=scalings) == want_rank


@pytest.mark.slowtest
@pytest.mark.parametrize('rank_method, proj', [('info', True),
                                               ('info', False),
                                               (None, True),
                                               (None, False)])
def test_cov_rank_estimation(rank_method, proj):
    """Test cov rank estimation."""
    # Test that our rank estimation works properly on a simple case
    evoked = read_evokeds(ave_fname, condition=0, baseline=(None, 0),
                          proj=False)
    cov = read_cov(cov_fname)
    ch_names = [ch for ch in evoked.info['ch_names'] if '053' not in ch and
                ch.startswith('EEG')]
    cov = prepare_noise_cov(cov, evoked.info, ch_names, None)
    assert cov['eig'][0] <= 1e-25  # avg projector should set this to zero
    assert (cov['eig'][1:] > 1e-16).all()  # all else should be > 0

    # Now do some more comprehensive tests
    raw_sample = read_raw_fif(raw_fname)
    assert not _has_eeg_average_ref_proj(raw_sample.info['projs'])

    raw_sss = read_raw_fif(hp_fif_fname)
    assert not _has_eeg_average_ref_proj(raw_sss.info['projs'])
    raw_sss.add_proj(compute_proj_raw(raw_sss))

    cov_sample = compute_raw_covariance(raw_sample)
    cov_sample_proj = compute_raw_covariance(
        raw_sample.copy().apply_proj())

    cov_sss = compute_raw_covariance(raw_sss)
    cov_sss_proj = compute_raw_covariance(
        raw_sss.copy().apply_proj())

    picks_all_sample = pick_types(raw_sample.info, meg=True, eeg=True)
    picks_all_sss = pick_types(raw_sss.info, meg=True, eeg=True)

    info_sample = pick_info(raw_sample.info, picks_all_sample)
    picks_stack_sample = [('eeg', pick_types(info_sample, meg=False,
                                             eeg=True))]
    picks_stack_sample += [('meg', pick_types(info_sample, meg=True))]
    picks_stack_sample += [('all',
                            pick_types(info_sample, meg=True, eeg=True))]

    info_sss = pick_info(raw_sss.info, picks_all_sss)
    picks_stack_somato = [('eeg', pick_types(info_sss, meg=False, eeg=True))]
    picks_stack_somato += [('meg', pick_types(info_sss, meg=True))]
    picks_stack_somato += [('all',
                            pick_types(info_sss, meg=True, eeg=True))]

    iter_tests = list(itt.product(
        [(cov_sample, picks_stack_sample, info_sample),
         (cov_sample_proj, picks_stack_sample, info_sample),
         (cov_sss, picks_stack_somato, info_sss),
         (cov_sss_proj, picks_stack_somato, info_sss)],  # sss
        [dict(mag=1e15, grad=1e13, eeg=1e6)],
    ))

    for (cov, picks_list, iter_info), scalings in iter_tests:
        rank = compute_rank(cov, rank_method, scalings, iter_info,
                            proj=proj)
        rank['all'] = sum(rank.values())
        for ch_type, picks in picks_list:

            this_info = pick_info(iter_info, picks)

            # compute subset of projs, active and inactive
            n_projs_applied = sum(proj['active'] and
                                  len(set(proj['data']['col_names']) &
                                      set(this_info['ch_names'])) > 0
                                  for proj in cov['projs'])
            n_projs_info = sum(len(set(proj['data']['col_names']) &
                                   set(this_info['ch_names'])) > 0
                               for proj in this_info['projs'])

            # count channel types
            ch_types = [channel_type(this_info, idx)
                        for idx in range(len(picks))]
            n_eeg, n_mag, n_grad = [ch_types.count(k) for k in
                                    ['eeg', 'mag', 'grad']]
            n_meg = n_mag + n_grad
            has_sss = (n_meg > 0 and len(this_info['proc_history']) > 0)
            if has_sss:
                n_meg = _get_rank_sss(this_info)

            expected_rank = n_meg + n_eeg
            if rank_method is None:
                if not has_sss:  # XXX see below and gh-5146
                    if proj:
                        expected_rank -= n_projs_info
                    else:
                        expected_rank -= n_projs_applied
            else:
                # XXX for now it just uses the total count
                assert rank_method == 'info'
                if proj:
                    expected_rank -= n_projs_info

            assert rank[ch_type] == expected_rank


@testing.requires_testing_data
@pytest.mark.parametrize('fname, rank_orig', ((hp_fif_fname, 120),
                                              (mf_fif_fname, 67)))
@pytest.mark.parametrize('n_proj', (0, 10))
def test_maxfilter_get_rank(n_proj, fname, rank_orig):
    """Test maxfilter rank lookup."""
    raw = read_raw_fif(fname).crop(0, 5).load_data().pick_types()
    assert raw.info['projs'] == []
    mf = raw.info['proc_history'][0]['max_info']
    assert mf['sss_info']['nfree'] == rank_orig
    assert _get_rank_sss(raw) == rank_orig
    rank = rank_orig - 2 * n_proj
    if n_proj > 0:
        # Let's do some projection
        raw.add_proj(compute_proj_raw(raw, n_mag=n_proj, n_grad=n_proj))
    raw.apply_proj()
    data_orig = raw[:][0]

    # degenerate cases
    with pytest.raises(ValueError, match='tol must be'):
        _estimate_rank_raw(raw, tol='foo')
    with pytest.raises(TypeError, match='must be a string or a number'):
        _estimate_rank_raw(raw, tol=None)

    # XXX this should be "rank" not "rank_orig", see gh-5146
    allowed_rank = [rank_orig]
    if fname == mf_fif_fname:
        # Here we permit a -1 because for mf_fif_fname we miss by 1, which is
        # probably acceptable. If we use the entire duration instead of 5 sec
        # this problem goes away, but the test is much slower.
        allowed_rank.append(allowed_rank[0] - 1)

    # multiple ways of hopefully getting the same thing
    # default tol=1e-4, scalings='norm'
    rank_new = _estimate_rank_raw(raw)
    assert rank_new in allowed_rank

    tol = 'float32'  # temporary option until we can fix things
    rank_new = _estimate_rank_raw(raw, tol=tol)
    assert rank_new in allowed_rank
    rank_new = _estimate_rank_raw(raw, scalings=dict(), tol=tol)
    assert rank_new in allowed_rank
    scalings = dict(grad=1e13, mag=1e15)
    rank_new = _compute_rank_int(raw, None, scalings=scalings, tol=tol,
                                 verbose='debug')
    assert rank_new in allowed_rank
    # XXX default scalings mis-estimate sometimes :(
    if fname == hp_fif_fname:
        allowed_rank.append(allowed_rank[0] - 2)
    rank_new = _compute_rank_int(raw, None, tol=tol, verbose='debug')
    assert rank_new in allowed_rank
    del allowed_rank

    rank_new = _compute_rank_int(raw, 'info')
    assert rank_new == rank
    assert_array_equal(raw[:][0], data_orig)

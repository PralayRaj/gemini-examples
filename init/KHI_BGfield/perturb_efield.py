from __future__ import annotations
import typing as T
import xarray
import numpy as np
from scipy.interpolate import interp1d, interp2d

import gemini3d.read
from gemini3d.config import datetime_range
from gemini3d.particles.grid import precip_grid
import gemini3d.conductivity


def perturb_efield(cfg: dict[str, T.Any], xg: dict[str, T.Any], params: dict[str, float] = None):
    """Electric field boundary conditions and initial condition for KHI case arguments"""

    if not params:
        #        params = {
        #            "v0": -500,
        #            # background flow value, actually this will be turned into a shear in the Efield input file
        #            "densfact": 3,
        #            # factor by which the density increases over the shear region - see Keskinen, et al (1988)
        #            "ell": 3.1513e3,  # scale length for shear transition
        #            "B1val": -50000e-9,
        #            "x1ref": 220e3,  # where to start tapering down the density in altitude
        #            "dx1": 10e3,
        #        }
        params = {
            "v0": 1000,
            # background flow value, actually this will be turned into a shear in the Efield input file
            "densfact": 4,
            # factor by which the density increases over the shear region - see Keskinen, et al (1988)
            "ell": 10e3,  # scale length for shear transition
            "B1val": -50000e-9,
            "x1ref": 220e3,  # where to start tapering down the density in altitude
            "dx1": 10e3,
        }

    params["vn"] = -params["v0"] * (1 + params["densfact"]) / (1 - params["densfact"])

    # %% Sizes
    x1 = xg["x1"][2:-2]
    x2 = xg["x2"][2:-2]
    lx2 = xg["lx"][1]
    lx3 = xg["lx"][2]

    # %% LOAD THE FRAME OF THE SIMULATION THAT WE WANT TO PERTURB
    dat = gemini3d.read.frame(cfg["indat_file"], var=["ns", "Ts", "v1"])

    nsscale = init_profile(xg, dat)

    nsperturb = perturb_density(xg, dat, nsscale, x1, x2, params)

    # %% compute initial potential, background
    # Phitop = potential_bg(x2, lx2, lx3, params)
    # Phitop = np.zeros( (lx2,lx3) )
    Phitop = 1 * np.random.randn(lx2, lx3)  # seed potential with random noise
    Phitop[0:9,:]=0.0
    Phitop[-10:-1,:]=0.0

    # %% Write initial plasma state out to a file
    gemini3d.write.state(
        cfg["indat_file"],
        dat,
        ns=nsperturb,
        Phitop=Phitop,
    )

    # %% Electromagnetic parameter inputs
    dat["ns"].data = nsperturb
    create_Efield(cfg, xg, dat, params)

    # create precipitation inputs
    # create_precip(cfg, xg, params)


def init_profile(xg: dict[str, T.Any], dat: xarray.Dataset):

    lsp = dat["ns"].shape[0]

    # %% Choose a single profile from the center of the eq domain
    ix2 = xg["lx"][1] // 2
    ix3 = xg["lx"][2] // 2

    nsscale = np.zeros_like(dat["ns"])
    for i in range(lsp):
        nprof = dat["ns"][i, :, ix2, ix3].values
        nsscale[i, :, :, :] = nprof[:, None, None]

    # %% SCALE EQ PROFILES UP TO SENSIBLE BACKGROUND CONDITIONS
    scalefact = 2 * 2.75 * 3
    for i in range(lsp - 1):
        nsscale[i, ...] = scalefact * nsscale[i, ...]

    nsscale[-1, ...] = nsscale[:-1, ...].sum(axis=0)
    # enforce quasineutrality

    return nsscale


def perturb_density(
    xg: dict[str, T.Any],
    dat: xarray.Dataset,
    nsscale: np.ndarray,
    x1: np.ndarray,
    x2: np.ndarray,
    params: dict[str, float],
):
    """
    because this is derived from current density it is invariant with respect
    to frame of reference.
    """
    lsp = dat["ns"].shape[0]

    nsperturb = np.zeros_like(dat["ns"])
    n1 = np.zeros_like(dat["ns"])
    for i in range(lsp):
        for ix2 in range(xg["lx"][1]):
            # 3D noise
            # amplitude = np.random.randn(xg["lx"][0], 1, xg["lx"][2])
            # AGWN
            # amplitude = 0.01*amplitude

            # 2D noise
            amplitude = np.random.randn(xg["lx"][2])
            amplitude = moving_average(amplitude, 10)
            amplitude = 0.01 * amplitude

            n1here = amplitude * nsscale[i, :, ix2, :]
            # perturbation seeding instability
            n1[i, :, ix2, :] = n1here
            # save the perturbation for computing potential perturbation

            # # 2-sided structure
            # nsperturb[i, :, ix2, :] = (
            #     nsscale[i, :, ix2, :]
            #     * (params["v0"] - params["vn"])
            #     / (params["v0"] * (np.tanh((x2[ix2]-50e3) / params["ell"]) - np.tanh((x2[ix2]+50e3)/params["ell"]) + 1)
            #         - params["vn"])
            # )

            # 1-sided structure
            nsperturb[i, :, ix2, :] = (
                nsscale[i, :, ix2, :]
                * (params["vn"] - params["v0"])
                / (params["v0"] * np.tanh((x2[ix2]) / params["ell"]) + params["vn"])
            )

            # background density
            nsperturb[i, :, ix2, :] = nsperturb[i, :, ix2, :] + n1here
            # perturbation

    nsperturb[nsperturb < 1e4] = 1e4
    # enforce a density floor
    # particularly need to pull out negative densities which can occur when noise is applied
    nsperturb[-1, :, :, :] = nsperturb[:6, :, :, :].sum(axis=0)
    # enforce quasineutrality
    n1[-1, :, :, :] = n1[:6, :, :, :].sum(axis=0)

    # %% Remove any residual E-region from the simulation other than what will
    #      be applied via precipitation
    taper = 1 / 2 + 1 / 2 * np.tanh((x1 - params["x1ref"]) / params["dx1"])
    for i in range(lsp - 1):
        for ix3 in range(xg["lx"][2]):
            for ix2 in range(xg["lx"][1]):
                nsperturb[i, :, ix2, ix3] = 1e6 + nsperturb[i, :, ix2, ix3] * taper

    inds = x1 < 150e3
    nsperturb[:, inds, :, :] = 1e3
    nsperturb[-1, :, :, :] = nsperturb[:6, :, :, :].sum(axis=0)
    # enforce quasineutrality

    return nsperturb


def potential_bg(x2: np.ndarray, lx2: int, lx3: int, params: dict[str, float]):

    vel3 = np.empty((lx2, lx3))
    for i in range(lx3):
        vel3[:, i] = (
            params["v0"]
            * (np.tanh((x2 - 50e3) / params["ell"]) - np.tanh((x2 + 50e3) / params["ell"]) + 1)
        ) - params["vn"]

    vel3 = np.flipud(vel3)
    # this is needed for consistentcy with equilibrium...  Not completely clear why
    E2top = vel3 * params["B1val"]
    # this is -1* the electric field

    # integrate field to get potential
    DX2 = np.diff(x2)
    DX2 = np.append(DX2, DX2[-1])

    Phitop = np.cumsum(E2top * DX2[:, None], axis=0)

    return Phitop


def create_Efield(cfg, xg, dat, params):

    cfg["E0dir"].mkdir(parents=True, exist_ok=True)

    # %% CREATE ELECTRIC FIELD DATASET
    llon = 512
    llat = 512
    # NOTE: cartesian-specific code
    if xg["lx"][1] == 1:
        llon = 1
    elif xg["lx"][2] == 1:
        llat = 1

    thetamin = xg["theta"].min()
    thetamax = xg["theta"].max()
    mlatmin = 90 - np.degrees(thetamax)
    mlatmax = 90 - np.degrees(thetamin)
    mlonmin = np.degrees(xg["phi"].min())
    mlonmax = np.degrees(xg["phi"].max())

    # add a 1% buff
    latbuf = 0.01 * (mlatmax - mlatmin)
    lonbuf = 0.01 * (mlonmax - mlonmin)

    E = xarray.Dataset(
        coords={
            "time": datetime_range(cfg["time"][0], cfg["time"][0] + cfg["tdur"], cfg["dtE0"]),
            "mlat": np.linspace(mlatmin - latbuf, mlatmax + latbuf, llat),
            "mlon": np.linspace(mlonmin - lonbuf, mlonmax + lonbuf, llon),
        }
    )
    Nt = E.time.size

    # %% INTERPOLATE X2 COORDINATE ONTO PROPOSED MLON GRID, assume Cartesian magnetic here I believe
    xgmlon = np.degrees(xg["phi"][0, :, 0])
    xgmlat = 90 - np.degrees(xg["theta"][0, 0, :])

    f = interp1d(xgmlon, xg["x2"][2 : xg["lx"][1] + 2], kind="linear", fill_value="extrapolate")
    x2i = f(E["mlon"])
    f = interp1d(xgmlat, xg["x3"][2 : xg["lx"][2] + 2], kind="linear", fill_value="extrapolate")
    x3i = f(E["mlat"])

    # compute an initial conductivity and conductance for specifying background current
    #   coordinates needed for later derivatives and integrals to define FAC
    #_, _, _, SigP, SigH, _, _ = gemini3d.conductivity.conductivity_reconstruct(
    #    cfg["time"][0], dat, cfg, xg
    #)
    #f = interp2d(xgmlat, xgmlon, SigP, kind="linear")
    #SigPi = f(E["mlon"], E["mlat"])
    #f = interp2d(xgmlat, xgmlon, SigH, kind="linear")
    #SigHi = f(E["mlat"], E["mlon"])

    # %% CREATE DATA FOR BACKGROUND ELECTRIC FIELDS
    if "Exit" in cfg:
        E["Exit"] = (("time", "mlon", "mlat"), cfg["Exit"] * np.ones((Nt, llon, llat)))
    else:
        E["Exit"] = (("time", "mlon", "mlat"), np.zeros((Nt, llon, llat)))
    if "Eyit" in cfg:
        E["Eyit"] = (("time", "mlon", "mlat"), cfg["Eyit"] * np.ones((Nt, llon, llat)))
    else:
        E["Eyit"] = (("time", "mlon", "mlat"), np.zeros((Nt, llon, llat)))

    # %% CREATE DATA FOR BOUNDARY CONDITIONS FOR POTENTIAL SOLUTION
    # if 0 data is interpreted as FAC, else we interpret it as potential
    E["flagdirich"] = (("time",), np.zeros(Nt, dtype=np.int32))
    E["Vminx1it"] = (("time", "mlon", "mlat"), np.zeros((Nt, llon, llat)))
    E["Vmaxx1it"] = (("time", "mlon", "mlat"), np.zeros((Nt, llon, llat)))
    # these are just slices
    E["Vminx2ist"] = (("time", "mlat"), np.zeros((Nt, llat)))
    E["Vmaxx2ist"] = (("time", "mlat"), np.zeros((Nt, llat)))
    E["Vminx3ist"] = (("time", "mlon"), np.zeros((Nt, llon)))
    E["Vmaxx3ist"] = (("time", "mlon"), np.zeros((Nt, llon)))

    for i in range(Nt):
        # ZEROS TOP CURRENT AND X3 BOUNDARIES DON'T MATTER SINCE PERIODIC

        # COMPUTE KHI DRIFT FROM APPLIED POTENTIAL
        vel3 = np.empty((llon, llat))
        for j in range(llat):
            vel3[:, j] = params["v0"] * np.tanh(x2i / params["ell"]) - params["vn"]
            #vel3[:, j] = params["v0"] * np.tanh(x2i / params["ell"])

        vel3 = np.flipud(vel3)

        # CONVERT TO ELECTRIC FIELD (actually -1* electric field...)
        E2slab = vel3 * params["B1val"]

        # At this point we can either store the results in the background field or
        #   integrate them and produce a boundary condition.  *should* be equivalent
        E["Exit"][i, :] = -1 * E2slab

        # # INTEGRATE TO PRODUCE A POTENTIAL OVER GRID - then save the edge boundary conditions
        # DX2 = np.diff(x2i)
        # DX2 = np.append(DX2, DX2[-1])
        # Phislab = np.cumsum(E2slab * DX2, axis=0)  # use a forward difference
        # E["Vmaxx2ist"][i, :] = Phislab[-1, :]  # drive through BCs
        # E["Vminx2ist"][i, :] = Phislab[0, :]  # drive through BCs

        # # Use FAC to enforce a smooth BG field, assume no E3, Cartesian, non-inverted for now
        # E["flagdirich"][i]=0
        # J2i=SigPi*E["Exit"][i,:,:]
        # J3i=SigHi*E["Exit"][i,:,:]

        # J2ix,_=np.gradient(J2i,x2i,x3i)
        # _,J3iy=np.gradient(J3i,x2i,x3i)
        # divJperp=J2ix+J3iy
        # E["Vmaxx1it"][i,:,:]=-1*divJperp

    # %% Write electric field data to file
    gemini3d.write.Efield(E, cfg["E0dir"])


def precip_SAID(pg, params, x2i, Qpeak, Qbackground):
    # mlon_mean = pg.mlon.mean().item()
    # mlat_mean = pg.mlat.mean().item()

    # if "mlon_sigma" in pg.attrs and "mlat_sigma" in pg.attrs:
    #     Q = (
    #         Qpeak
    #         * np.exp(-((pg.mlon.data[:, None] - mlon_mean) ** 2) / (2 * pg.mlon_sigma ** 2))
    #         * np.exp(-((pg.mlat.data[None, :] - mlat_mean) ** 2) / (2 * pg.mlat_sigma ** 2))
    #     )
    # elif "mlon_sigma" in pg.attrs:
    #     Q = Qpeak * np.exp(-((pg.mlon.data[:, None] - mlon_mean) ** 2) / (2 * pg.mlon_sigma ** 2))
    # elif "mlat_sigma" in pg.attrs:
    #     Q = Qpeak * np.exp(-((pg.mlat.data[None, :] - mlat_mean) ** 2) / (2 * pg.mlat_sigma ** 2))
    # else:
    #     raise LookupError("precipation must be defined in latitude, longitude or both")

    Q = Qpeak * (1 / 2 * np.tanh((x2i[:, None] - 50e3) / params["ell"]) + 1 / 2)
    Q[Q < Qbackground] = Qbackground

    return Q


def create_precip(cfg, xg, params):
    """write particle precipitation to disk"""

    # %% CREATE PRECIPITATION INPUT DATA
    # Q: energy flux [mW m^-2]
    # E0: characteristic energy [eV]

    pg = precip_grid(cfg, xg)

    # did user specify on/off time? if not, assume always on.
    t0 = pg.time[0].data

    if "precip_startsec" in cfg:
        t = t0 + np.timedelta64(cfg["precip_startsec"])
        i_on = abs(pg.time - t).argmin().item()
    else:
        i_on = 0

    if "precip_endsec" in cfg:
        t = t0 + np.timedelta64(cfg["precip_endsec"])
        i_off = abs(pg.time - t).argmin().item()
    else:
        i_off = pg.time.size

    assert np.isfinite(cfg["E0precip"]), "E0 precipitation must be finite"
    assert cfg["E0precip"] > 0, "E0 precip must be positive"
    assert cfg["E0precip"] < 100e6, "E0 precip must not be relativistic 100 MeV"

    llon = 512
    llat = 512
    # NOTE: cartesian-specific code
    if xg["lx"][1] == 1:
        llon = 1
    elif xg["lx"][2] == 1:
        llat = 1

    thetamin = xg["theta"].min()
    thetamax = xg["theta"].max()
    mlatmin = 90 - np.degrees(thetamax)
    mlatmax = 90 - np.degrees(thetamin)
    mlonmin = np.degrees(xg["phi"].min())
    mlonmax = np.degrees(xg["phi"].max())

    # add a 1% buff
    latbuf = 0.01 * (mlatmax - mlatmin)
    lonbuf = 0.01 * (mlonmax - mlonmin)

    time = datetime_range(cfg["time"][0], cfg["time"][0] + cfg["tdur"], cfg["dtprec"])

    pg = xarray.Dataset(
        {
            "Q": (("time", "mlon", "mlat"), np.zeros((len(time), llon, llat))),
            "E0": (("time", "mlon", "mlat"), np.zeros((len(time), llon, llat))),
        },
        coords={
            "time": time,
            "mlat": np.linspace(mlatmin - latbuf, mlatmax + latbuf, llat),
            "mlon": np.linspace(mlonmin - lonbuf, mlonmax + lonbuf, llon),
        },
    )
    Nt = pg.time.size

    # %% INTERPOLATE X2 COORDINATE ONTO PROPOSED MLON GRID
    xgmlon = np.degrees(xg["phi"][0, :, 0])
    # xgmlat = 90 - np.degrees(xg["theta"][0, 0, :])

    f = interp1d(xgmlon, xg["x2"][2 : xg["lx"][1] + 2], kind="linear", fill_value="extrapolate")
    x2i = f(pg["mlon"])
    # f = interp1d(xgmlat, xg["x3"][2:lx3 + 2], kind='linear', fill_value="extrapolate")
    # x3i = f(E["mlat"])

    # NOTE: in future, E0 could be made time-dependent in config.nml as 1D array
    for i in range(i_on, i_off):
        pg["Q"][i, :, :] = precip_SAID(pg, params, x2i, cfg["Qprecip"], cfg["Qprecip_background"])
        pg["E0"][i, :, :] = cfg["E0precip"]

    assert np.isfinite(pg["Q"]).all(), "Q flux must be finite"
    assert (pg["Q"] >= 0).all(), "Q flux must be non-negative"

    gemini3d.write.precip(pg, cfg["precdir"])


def moving_average(x, k: int):
    # https://stackoverflow.com/a/54628145
    return np.convolve(x, np.ones(k), mode="same") / k

#!/usr/bin/env python3
"""Benchmark DWSIM against Wang et al. 2022 published Aspen Plus results.

Builds DWSIM distillation column models matching the column specifications
from Wang et al. 2022 (FED 184:113078) and compares outputs against the
published product compositions, temperatures, and heat loads.

Two benchmark modes:
  --mode issi-cd2 : Single-column ISS-I CD2 (75 stages, NBI feed)
  --mode isso     : Three-column ISS-O cascade (H2/HD/HT system)

Usage:
    cd /path/to/tricys
    DOTNET_ROOT=/usr/lib/dotnet python script/dwsim/benchmark_wang2022.py --mode issi-cd2
"""

import argparse
import json
import os
import sys

DWSIM_DIR = os.environ.get("DWSIM_DIR", "/usr/local/lib/dwsim")
DOTNET_ROOT = os.environ.get("DOTNET_ROOT", "/usr/lib/dotnet")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.join(SCRIPT_DIR, "..", "..")

# DWSIM compound names
DWSIM_COMPOUNDS = ["Hydrogen", "HD", "Deuterium", "HT", "DT", "Tritium"]

# Map from Wang et al. species names to DWSIM compound names
WANG_TO_DWSIM = {
    "H2": "Hydrogen",
    "HD": "HD",
    "D2": "Deuterium",
    "HT": "HT",
    "DT": "DT",
    "T2": "Tritium",
}

# Minimum molar flow for "zero" components to avoid flash calculation issues
EPSILON_MOL_S = 1e-15


def load_fixture(fixture_path):
    """Load benchmark fixture JSON file."""
    with open(fixture_path, "r") as f:
        return json.load(f)


def setup_dwsim():
    """Initialize DWSIM runtime and return Automation3 interface."""
    os.environ["DOTNET_ROOT"] = DOTNET_ROOT

    from pythonnet import set_runtime
    from clr_loader import get_coreclr

    rt = get_coreclr(dotnet_root=DOTNET_ROOT)
    set_runtime(rt)

    import clr
    sys.path.append(DWSIM_DIR)
    clr.AddReference("DWSIM.Automation")
    clr.AddReference("DWSIM.Interfaces")

    from DWSIM.Automation import Automation3
    return Automation3()


def create_base_flowsheet(interf):
    """Create flowsheet with registered compounds and SRK property package."""
    sys.path.insert(0, SCRIPT_DIR)
    from register_compounds import register_compounds
    from build_dwsim_flowsheet import configure_srk_bip

    sim = interf.CreateFlowsheet()
    register_compounds(sim, {})
    sim.CreateAndAddPropertyPackage("Soave-Redlich-Kwong (SRK)")
    configure_srk_bip(sim, {})
    return sim


def _set_feed_composition(stream_obj, total_flow_mol_h, composition):
    """Set feed stream molar flows from Wang et al. composition data.

    Args:
        stream_obj: DWSIM MaterialStream object
        total_flow_mol_h: Total molar flow in mol/h
        composition: dict {species_name: mole_fraction} using Wang names (H2/HD/D2/HT/DT/T2)
    """
    total_mol_s = total_flow_mol_h / 3600.0

    for wang_name, dwsim_name in WANG_TO_DWSIM.items():
        frac = composition.get(wang_name, 0.0)
        mol_flow = frac * total_mol_s
        if mol_flow < EPSILON_MOL_S:
            mol_flow = EPSILON_MOL_S
        stream_obj.SetOverallCompoundMolarFlow(dwsim_name, mol_flow)


def build_single_column(sim, fixture):
    """Build a single distillation column from fixture data.

    Args:
        sim: DWSIM flowsheet (with compounds and property package)
        fixture: Loaded JSON fixture dict (wang2022_issi_cd2.json schema)

    Returns:
        dict {"column": dc_obj, "streams": {"feed": ..., "distillate": ..., "bottoms": ...},
              "energy": {"condenser": ..., "reboiler": ...}}
    """
    from DWSIM.Interfaces.Enums.GraphicObjects import ObjectType

    feed_data = fixture["feed"]
    col_data = fixture["column"]

    # Create feed stream
    feed = sim.AddObject(ObjectType.MaterialStream, 50, 200, "FEED")
    feed_obj = feed.GetAsObject()
    feed_obj.SetTemperature(22.0)  # Initial estimate (K)
    feed_obj.SetPressure(col_data["pressure_top_Pa"])
    _set_feed_composition(
        feed_obj,
        feed_data["total_flow_mol_h"],
        feed_data["composition_mole_fraction"],
    )

    # Create output streams
    dist = sim.AddObject(ObjectType.MaterialStream, 300, 50, "DISTILLATE")
    dist_obj = dist.GetAsObject()

    bott = sim.AddObject(ObjectType.MaterialStream, 300, 350, "BOTTOMS")
    bott_obj = bott.GetAsObject()

    # Create energy streams
    qc = sim.AddObject(ObjectType.EnergyStream, 400, 30, "QC")
    qc_obj = qc.GetAsObject()

    qr = sim.AddObject(ObjectType.EnergyStream, 400, 370, "QR")
    qr_obj = qr.GetAsObject()

    # Create distillation column
    dc = sim.AddObject(ObjectType.DistillationColumn, 200, 200, col_data["name"])
    dc_obj = dc.GetAsObject()

    dc_obj.SetNumberOfStages(col_data["total_stages"])
    dc_obj.SetTopPressure(col_data["pressure_top_Pa"])
    dc_obj.ColumnPressureDrop = (
        col_data["pressure_bottom_Pa"] - col_data["pressure_top_Pa"]
    )

    # Convergence settings — relaxed for high stage count
    dc_obj.ExternalLoopTolerance = 0.001
    dc_obj.InternalLoopTolerance = 0.001
    dc_obj.MaxIterations = 500

    # Set column specs: reflux ratio + bottoms flow rate
    dc_obj.SetCondenserSpec("R", col_data["reflux_ratio"], "mol/mol", "")

    # Calculate bottoms flow from D/F ratio
    total_feed = feed_data["total_flow_mol_h"] / 3600.0  # mol/s
    d_f = col_data["distillate_to_feed_ratio"]
    bottoms_flow = total_feed * (1.0 - d_f)
    dc_obj.SetReboilerSpec("B", bottoms_flow, "mol/s", "")

    # Connect streams
    dc_obj.ConnectFeed(feed_obj, col_data["feed_stage"])
    dc_obj.ConnectDistillate(dist_obj)
    dc_obj.ConnectBottoms(bott_obj)
    dc_obj.ConnectCondenserDuty(qc_obj)
    dc_obj.ConnectReboilerDuty(qr_obj)

    print(f"  Built {col_data['name']}: {col_data['total_stages']} stages, "
          f"R={col_data['reflux_ratio']}, feed@{col_data['feed_stage']}")

    return {
        "column": dc_obj,
        "streams": {"feed": feed_obj, "distillate": dist_obj, "bottoms": bott_obj},
        "energy": {"condenser": qc_obj, "reboiler": qr_obj},
    }


def build_isso_three_columns(sim, fixture):
    """Build ISS-O three-column cascade from fixture data.

    Simplified topology (no equilibrator):
        WDS → CD1 → top: H2 exhaust
                   → bottom → CD2 (stage 35)
        TES → CD2 (stage 20)
                   → top: H2 exhaust
                   → bottom → CD3 (stage 30, skipping equilibrator)
        CD3 → top → CD2 (stage 45) [recycle]
            → bottom: T2 product

    Note: CD2_bottom → CD1 recycle (stage 15) is omitted initially
    to avoid convergence issues with the recycle loop.

    Args:
        sim: DWSIM flowsheet (with compounds and property package)
        fixture: Loaded JSON fixture dict (wang2022_isso.json schema)

    Returns:
        dict {"columns": {name: obj}, "streams": {name: obj}, "energy": {name: obj}}
    """
    from DWSIM.Interfaces.Enums.GraphicObjects import ObjectType

    feeds_data = fixture["feeds"]
    cols_data = fixture["columns"]

    objects = {"columns": {}, "streams": {}, "energy": {}}

    # --- Create external feed streams ---
    wds = sim.AddObject(ObjectType.MaterialStream, 50, 150, "WDS_FEED")
    wds_obj = wds.GetAsObject()
    wds_obj.SetTemperature(20.0)
    wds_obj.SetPressure(cols_data["CD1"]["pressure_top_Pa"])
    _set_feed_composition(
        wds_obj,
        feeds_data["WDS"]["total_flow_mol_h"],
        feeds_data["WDS"]["composition_mole_fraction"],
    )
    objects["streams"]["WDS_FEED"] = wds_obj

    tes = sim.AddObject(ObjectType.MaterialStream, 50, 250, "TES_FEED")
    tes_obj = tes.GetAsObject()
    tes_obj.SetTemperature(20.0)
    tes_obj.SetPressure(cols_data["CD2"]["pressure_top_Pa"])
    _set_feed_composition(
        tes_obj,
        feeds_data["TES"]["total_flow_mol_h"],
        feeds_data["TES"]["composition_mole_fraction"],
    )
    objects["streams"]["TES_FEED"] = tes_obj

    # --- Create inter-column and product streams ---
    stream_defs = {
        "CD1_TOP": (250, 50),    # H2 exhaust
        "CD1_BOTT": (250, 350),  # -> CD2 stage 35
        "CD2_TOP": (500, 50),    # H2 exhaust
        "CD2_BOTT": (500, 350),  # -> CD3 stage 30 (skip equilibrator)
        "CD3_TOP": (750, 50),    # recycle to CD2 stage 45
        "CD3_BOTT": (750, 350),  # T2 product
    }
    for name, (x, y) in stream_defs.items():
        s = sim.AddObject(ObjectType.MaterialStream, x, y, name)
        objects["streams"][name] = s.GetAsObject()

    # --- Create energy streams ---
    for col_name in ["CD1", "CD2", "CD3"]:
        for duty_type, y in [("QC", 30), ("QR", 370)]:
            label = f"{duty_type}_{col_name}"
            e = sim.AddObject(ObjectType.EnergyStream, 300, y, label)
            objects["energy"][label] = e.GetAsObject()

    # --- Create and configure columns ---
    col_x_offsets = {"CD1": 200, "CD2": 450, "CD3": 700}

    for col_name, cfg in cols_data.items():
        x = col_x_offsets[col_name]
        dc = sim.AddObject(ObjectType.DistillationColumn, x, 200, col_name)
        dc_obj = dc.GetAsObject()

        dc_obj.SetNumberOfStages(cfg["total_stages"])
        dc_obj.SetTopPressure(cfg["pressure_top_Pa"])
        dc_obj.ColumnPressureDrop = cfg["pressure_bottom_Pa"] - cfg["pressure_top_Pa"]

        dc_obj.ExternalLoopTolerance = 0.005
        dc_obj.InternalLoopTolerance = 0.005
        dc_obj.MaxIterations = 500

        dc_obj.SetCondenserSpec("R", cfg["reflux_ratio"], "mol/mol", "")

        # Estimate bottoms flow from D/F ratio
        # For CD1: feed is WDS (280 mol/h)
        # For CD2: feed is TES (160) + CD1_BOTT, approximate
        # For CD3: feed is CD2_BOTT, approximate
        if col_name == "CD1":
            total_feed = feeds_data["WDS"]["total_flow_mol_h"] / 3600.0
        elif col_name == "CD2":
            total_feed = (feeds_data["TES"]["total_flow_mol_h"] + 5.0) / 3600.0
        else:
            total_feed = 3.0 / 3600.0  # Approximate CD3 feed

        bottoms_flow = total_feed * (1.0 - cfg["distillate_to_feed_ratio"])
        dc_obj.SetReboilerSpec("B", bottoms_flow, "mol/s", "")

        objects["columns"][col_name] = dc_obj
        print(f"  Built {col_name}: {cfg['total_stages']} stages, R={cfg['reflux_ratio']}")

    # --- Connect streams ---
    # CD1
    objects["columns"]["CD1"].ConnectFeed(objects["streams"]["WDS_FEED"], 20)
    objects["columns"]["CD1"].ConnectDistillate(objects["streams"]["CD1_TOP"])
    objects["columns"]["CD1"].ConnectBottoms(objects["streams"]["CD1_BOTT"])
    objects["columns"]["CD1"].ConnectCondenserDuty(objects["energy"]["QC_CD1"])
    objects["columns"]["CD1"].ConnectReboilerDuty(objects["energy"]["QR_CD1"])

    # CD2: feeds = TES (stage 20) + CD1_BOTT (stage 35)
    objects["columns"]["CD2"].ConnectFeed(objects["streams"]["TES_FEED"], 20)
    try:
        objects["columns"]["CD2"].ConnectFeed(objects["streams"]["CD1_BOTT"], 35)
        print("  CD2: multi-feed via ConnectFeed succeeded")
    except Exception as e:
        print(f"  CD2: multi-feed ConnectFeed failed ({e}), using single feed")
    objects["columns"]["CD2"].ConnectDistillate(objects["streams"]["CD2_TOP"])
    objects["columns"]["CD2"].ConnectBottoms(objects["streams"]["CD2_BOTT"])
    objects["columns"]["CD2"].ConnectCondenserDuty(objects["energy"]["QC_CD2"])
    objects["columns"]["CD2"].ConnectReboilerDuty(objects["energy"]["QR_CD2"])

    # CD3: feed = CD2_BOTT (stage 30, no equilibrator)
    objects["columns"]["CD3"].ConnectFeed(objects["streams"]["CD2_BOTT"], 30)
    objects["columns"]["CD3"].ConnectDistillate(objects["streams"]["CD3_TOP"])
    objects["columns"]["CD3"].ConnectBottoms(objects["streams"]["CD3_BOTT"])
    objects["columns"]["CD3"].ConnectCondenserDuty(objects["energy"]["QC_CD3"])
    objects["columns"]["CD3"].ConnectReboilerDuty(objects["energy"]["QR_CD3"])

    # CD3 top recycle to CD2 stage 45 — try to connect
    try:
        objects["columns"]["CD2"].ConnectFeed(objects["streams"]["CD3_TOP"], 45)
        print("  CD2: CD3_TOP recycle feed connected at stage 45")
    except Exception as e:
        print(f"  CD2: CD3_TOP recycle connection failed ({e}), omitting recycle")

    print("  Topology: WDS→CD1, TES+CD1_BOTT→CD2, CD2_BOTT→CD3, CD3_TOP→CD2 (recycle)")
    return objects


def extract_column_results(sim, interf, column_obj, dist_stream, bott_stream,
                           qc_stream=None, qr_stream=None):
    """Extract column results after solving.

    Returns:
        dict with keys: converged, top_composition, bottom_composition,
                        temperatures, heat_loads
    """
    converged = column_obj.Calculated

    def _get_composition(stream_obj):
        """Extract mole fractions from a material stream."""
        comp = {}
        phases = stream_obj.Phases
        overall = phases[0]
        for wang_name, dwsim_name in WANG_TO_DWSIM.items():
            try:
                mf = overall.Compounds[dwsim_name].MoleFraction
                comp[wang_name] = float(mf) if mf is not None else 0.0
            except Exception:
                comp[wang_name] = 0.0
        return comp

    def _get_stream_flow(stream_obj):
        """Get total molar flow in mol/h."""
        try:
            return stream_obj.GetMolarFlow() * 3600.0
        except Exception:
            return 0.0

    top_comp = _get_composition(dist_stream)
    bottom_comp = _get_composition(bott_stream)
    top_flow = _get_stream_flow(dist_stream)
    bottom_flow = _get_stream_flow(bott_stream)

    # Get temperatures from column stages
    try:
        stages = column_obj.Stages
        n_stages = stages.Count
        top_T = stages[0].T  # Condenser temperature
        bot_T = stages[n_stages - 1].T  # Reboiler temperature
    except Exception:
        top_T = 0.0
        bot_T = 0.0

    # Get heat loads from energy streams
    cond_duty = 0.0
    reb_duty = 0.0
    if qc_stream is not None:
        try:
            cond_duty = qc_stream.EnergyFlow  # W or kW depending on DWSIM
        except Exception:
            pass
    if qr_stream is not None:
        try:
            reb_duty = qr_stream.EnergyFlow
        except Exception:
            pass

    return {
        "converged": converged,
        "top_composition": top_comp,
        "bottom_composition": bottom_comp,
        "top_flow_mol_h": top_flow,
        "bottom_flow_mol_h": bottom_flow,
        "temperatures": {"top_K": top_T, "bottom_K": bot_T},
        "heat_loads": {"condenser_W": cond_duty, "reboiler_W": reb_duty},
    }


def compare_with_expected(actual, expected, tolerances):
    """Compare actual results with expected values from fixture.

    Returns:
        list of dicts: [{metric, expected, actual, deviation, pass_fail}, ...]
    """
    rows = []

    # Compare compositions
    for loc, loc_key in [("top", "top_composition_mole_fraction"),
                         ("bottom", "bottom_composition_mole_fraction")]:
        expected_comp = expected.get(loc_key, {})
        actual_comp = actual.get(f"{loc}_composition", {})
        for species, exp_val in expected_comp.items():
            if species == "notes":
                continue
            if not isinstance(exp_val, (int, float)):
                continue
            act_val = actual_comp.get(species, 0.0)
            if abs(exp_val) > 1e-10:
                dev = abs(act_val - exp_val) / abs(exp_val)
            else:
                dev = abs(act_val)
            tol = tolerances.get("composition_rel", 0.15)
            passed = dev < tol or abs(act_val - exp_val) < 1e-6
            rows.append({
                "metric": f"{loc}_{species}",
                "expected": exp_val,
                "actual": act_val,
                "deviation": dev,
                "pass": passed,
            })

    # Compare temperatures
    exp_temps = expected.get("temperatures_K", {})
    act_temps = actual.get("temperatures", {})
    for pos in ["top", "bottom"]:
        exp_val = exp_temps.get(pos)
        act_val = act_temps.get(f"{pos}_K", 0.0)
        if exp_val is not None and isinstance(exp_val, (int, float)):
            dev = abs(act_val - exp_val)
            tol = tolerances.get("temperature_abs_K", 1.0)
            rows.append({
                "metric": f"T_{pos}",
                "expected": exp_val,
                "actual": act_val,
                "deviation": dev,
                "pass": dev < tol,
            })

    # Compare heat loads
    exp_hl = expected.get("heat_loads_W", {})
    act_hl = actual.get("heat_loads", {})
    for duty in ["condenser", "reboiler"]:
        exp_val = exp_hl.get(duty)
        act_val = act_hl.get(f"{duty}_W", 0.0)
        if exp_val is not None and isinstance(exp_val, (int, float)):
            if abs(exp_val) > 1e-6:
                dev = abs(act_val - exp_val) / abs(exp_val)
            else:
                dev = abs(act_val)
            tol = tolerances.get("heat_load_rel", 0.25)
            rows.append({
                "metric": f"Q_{duty}",
                "expected": exp_val,
                "actual": act_val,
                "deviation": dev,
                "pass": dev < tol,
            })

    return rows


def print_deviation_table(rows, title=""):
    """Print formatted deviation table to console."""
    if title:
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}")
    print(f"{'Metric':<20} {'Expected':>12} {'Actual':>12} {'Deviation':>12} {'Status':>8}")
    print("-" * 68)
    for row in rows:
        exp_str = f"{row['expected']:.6g}" if isinstance(row["expected"], float) else str(row["expected"])
        act_str = f"{row['actual']:.6g}" if isinstance(row["actual"], float) else str(row["actual"])
        if isinstance(row["deviation"], float):
            if row["metric"].startswith("T_"):
                dev_str = f"{row['deviation']:.3f} K"
            else:
                dev_str = f"{row['deviation']:.2%}"
        else:
            dev_str = str(row["deviation"])
        status = "PASS" if row["pass"] else "FAIL"
        print(f"{row['metric']:<20} {exp_str:>12} {act_str:>12} {dev_str:>12} {status:>8}")

    pass_count = sum(1 for r in rows if r["pass"])
    total = len(rows)
    print(f"\nPass rate: {pass_count}/{total} ({100*pass_count/total:.0f}%)")
    return pass_count, total


def run_issi_cd2_benchmark(fixture_path=None):
    """Run ISS-I CD2 single-column benchmark.

    Returns:
        dict with keys: actual_results, comparison_table, pass_count, total_count
    """
    if fixture_path is None:
        fixture_path = os.path.join(
            PROJECT_ROOT, "test", "dwsim", "fixtures", "wang2022_issi_cd2.json"
        )

    print("Loading ISS-I CD2 fixture...")
    fixture = load_fixture(fixture_path)

    print("Initializing DWSIM...")
    interf = setup_dwsim()
    sim = create_base_flowsheet(interf)

    print("Building ISS-I CD2 single column...")
    built = build_single_column(sim, fixture)

    print("Solving flowsheet...")
    converged = False
    for attempt in range(10):
        try:
            interf.CalculateFlowsheet2(sim)
        except Exception as e:
            print(f"  Solve exception (attempt {attempt+1}): {e}")
            continue

        if built["column"].Calculated:
            converged = True
            print(f"  Converged on attempt {attempt+1}")
            break
        else:
            print(f"  Not converged (attempt {attempt+1}), retrying...")

    if not converged:
        print("  WARNING: Column did not converge after 10 attempts")
        # Try with relaxed tolerances
        print("  Retrying with relaxed tolerances...")
        built["column"].ExternalLoopTolerance = 0.01
        built["column"].InternalLoopTolerance = 0.01
        for attempt in range(5):
            try:
                interf.CalculateFlowsheet2(sim)
            except Exception:
                continue
            if built["column"].Calculated:
                converged = True
                print(f"  Converged with relaxed tolerance (attempt {attempt+1})")
                break

    # Extract results
    actual = extract_column_results(
        sim, interf, built["column"],
        built["streams"]["distillate"],
        built["streams"]["bottoms"],
        built["energy"]["condenser"],
        built["energy"]["reboiler"],
    )
    actual["converged"] = converged

    # Compare with expected
    comparison = compare_with_expected(
        actual,
        fixture["expected_results"],
        fixture["tolerances"],
    )

    pass_count, total_count = print_deviation_table(
        comparison, "ISS-I CD2 Benchmark: DWSIM vs Wang et al. 2022"
    )

    # Save results
    results_path = os.path.join(
        PROJECT_ROOT, "test", "dwsim", "fixtures", "wang2022_issi_cd2_results.json"
    )
    results = {
        "converged": converged,
        "actual_results": actual,
        "comparison": comparison,
        "pass_count": pass_count,
        "total_count": total_count,
    }
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {results_path}")

    return results


def run_isso_benchmark(fixture_path=None):
    """Run ISS-O three-column benchmark.

    Returns:
        dict with keys per column: actual_results, comparison_table, pass/total
    """
    if fixture_path is None:
        fixture_path = os.path.join(
            PROJECT_ROOT, "test", "dwsim", "fixtures", "wang2022_isso.json"
        )

    print("Loading ISS-O fixture...")
    fixture = load_fixture(fixture_path)

    print("Initializing DWSIM...")
    interf = setup_dwsim()
    sim = create_base_flowsheet(interf)

    print("Building ISS-O three-column cascade...")
    built = build_isso_three_columns(sim, fixture)

    print("Solving flowsheet (multi-column, may need multiple iterations)...")
    any_converged = False
    for attempt in range(15):
        try:
            interf.CalculateFlowsheet2(sim)
        except Exception as e:
            print(f"  Solve exception (attempt {attempt+1}): {e}")
            continue

        conv_status = {
            name: obj.Calculated for name, obj in built["columns"].items()
        }
        n_conv = sum(conv_status.values())
        print(f"  Attempt {attempt+1}: {n_conv}/3 columns converged {conv_status}")
        if n_conv == 3:
            any_converged = True
            break
        if n_conv >= 2 and attempt > 8:
            any_converged = True
            print(f"  Accepting {n_conv}/3 convergence after {attempt+1} attempts")
            break

    # Extract and compare results per column
    stream_map = {
        "CD1": ("CD1_TOP", "CD1_BOTT"),
        "CD2": ("CD2_TOP", "CD2_BOTT"),
        "CD3": ("CD3_TOP", "CD3_BOTT"),
    }

    all_results = {}
    total_pass = 0
    total_count = 0

    for col_name, (top_key, bott_key) in stream_map.items():
        actual = extract_column_results(
            sim, interf,
            built["columns"][col_name],
            built["streams"][top_key],
            built["streams"][bott_key],
            built["energy"].get(f"QC_{col_name}"),
            built["energy"].get(f"QR_{col_name}"),
        )

        # Build expected for this column
        expected = {}
        exp_results = fixture["expected_results"]

        # Temperatures
        if col_name in exp_results.get("temperatures_K", {}):
            expected["temperatures_K"] = exp_results["temperatures_K"][col_name]

        # Heat loads
        if col_name in exp_results.get("heat_loads_W", {}):
            expected["heat_loads_W"] = exp_results["heat_loads_W"][col_name]

        # Compositions
        prod_comp = exp_results.get("product_compositions", {}).get(col_name, {})
        if "top" in prod_comp:
            top_mf = {}
            for species, data in prod_comp["top"].items():
                if isinstance(data, dict) and "mole_fraction" in data:
                    top_mf[species] = data["mole_fraction"]
            if top_mf:
                expected["top_composition_mole_fraction"] = top_mf

        if "bottom" in prod_comp:
            bottom_mf = {}
            for species, data in prod_comp["bottom"].items():
                if isinstance(data, dict) and "mole_fraction" in data:
                    bottom_mf[species] = data["mole_fraction"]
            if bottom_mf:
                expected["bottom_composition_mole_fraction"] = bottom_mf

        # Use wider tolerance for CD3 (no equilibrator)
        tol = dict(fixture["tolerances"])
        if col_name == "CD3":
            tol["composition_rel"] = tol.get("composition_rel_simplified", 0.25)

        comparison = compare_with_expected(actual, expected, tol)
        pc, tc = print_deviation_table(
            comparison, f"ISS-O {col_name}: DWSIM vs Wang et al. 2022"
        )
        total_pass += pc
        total_count += tc

        all_results[col_name] = {
            "converged": actual["converged"],
            "actual_results": actual,
            "comparison": comparison,
            "pass_count": pc,
            "total_count": tc,
        }

    print(f"\n{'='*60}")
    print(f"  ISS-O Overall: {total_pass}/{total_count} "
          f"({100*total_pass/total_count:.0f}%)")
    print(f"{'='*60}")

    # Save results
    results_path = os.path.join(
        PROJECT_ROOT, "test", "dwsim", "fixtures", "wang2022_isso_results.json"
    )
    with open(results_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nResults saved to {results_path}")

    return all_results


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark DWSIM against Wang et al. 2022 published results"
    )
    parser.add_argument(
        "--mode", choices=["issi-cd2", "isso"], required=True,
        help="Benchmark mode: issi-cd2 (single column) or isso (three columns)"
    )
    parser.add_argument("--fixture", help="Override fixture JSON path")
    args = parser.parse_args()

    if args.mode == "issi-cd2":
        results = run_issi_cd2_benchmark(args.fixture)
        sys.exit(0 if results.get("converged") else 1)
    elif args.mode == "isso":
        results = run_isso_benchmark(args.fixture)
        any_converged = any(r.get("converged") for r in results.values())
        sys.exit(0 if any_converged else 1)


if __name__ == "__main__":
    main()

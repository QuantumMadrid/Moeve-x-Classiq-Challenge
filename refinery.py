"""Mini Refinery Horizon: plant model and plan evaluator.

This module is the "physics" of the challenge. It does NOT optimize anything.
Given an operating plan, it returns the margin, the mass flows, the blended
qualities and every constraint violation. Use it to

* check that your quantum and classical solutions are correct,
* build cost functions / oracles,
* count how many times your method had to evaluate a plan.

Conventions (from Moeve's case, Appendix A, simplified):
* All flows in kt over one 30-day period, prices in EUR/t, so margin is in kEUR.
* Yields and qualities blend linearly by mass.
* The ADO/Residue cut temperature sets alpha in [0, 1]: the fraction of the
  ADO/Residue swing cut sent UP into diesel (ADO). The rest goes DOWN into residue.
* The Naphtha/Kerosene and Kerosene/ADO boundaries are fixed at their midpoint
  (50/50 split). Everything lighter than ADO is sold as "light products".
* All ADO from all crudes enters ONE perfect-mixing distillate pool. Every
  outlet of the pool has the same composition.
* Pool outlets: Premium Diesel, Standard Diesel, and the hydrocracker
  (everything not sold as diesel). The imported Premium Diesel component can
  only be blended into Premium Diesel.
* Intermediate and Full-on instances add a Diesel HDS (hydrodesulphurizer)
  between the pool and blending. It treats the whole pool. Its outlet sulphur
  and yield follow the nonlinear Kero-HDS relation of Moeve's case
  (Appendix B5), applied here to the diesel pool. Severity "bypass" (None)
  switches the unit off, so the Easy plant is a special case.
* Mixed-integer mode: the operating mode (crudes, cut temperatures, CDUs, HDS
  severity) is discrete; the pool split and the import are continuous. Once the
  mode is fixed, the best continuous completion is a small LP (`complete_plan`).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
TOL = 1e-6  # numerical tolerance on constraints (kt or kt*quality units)


INSTANCE_FILES = {
    "easy": "easy.json",  # Levels 0 and 1
    "intermediate": "intermediate.json",  # Level 2
    "full_on": "full_on.json",  # Level 3a
}


def load_instance(level: str = "easy") -> dict:
    """Load a challenge instance: "easy", "intermediate", "full_on", or a path to a JSON file."""
    if level.endswith(".json"):
        path = Path(level)
    elif level in INSTANCE_FILES:
        path = DATA_DIR / INSTANCE_FILES[level]
    else:
        raise ValueError(f"unknown instance {level!r}; choose from {list(INSTANCE_FILES)} or a .json path")
    with open(path) as f:
        return json.load(f)


@dataclass
class Plan:
    """A refinery operating plan in physical units.

    crude_kt:        crude purchased and processed, kt per crude
    alpha:           ADO/Residue swing fraction sent up to ADO, per crude, in [0, 1]
    cdu:             CDU each crude runs on (only needed when a crude has a choice)
    premium_share:   fraction of the distillate pool sent to Premium Diesel
    standard_share:  fraction of the distillate pool sent to Standard Diesel
    import_kt:       imported Premium Diesel component, kt
    hds_severity:    Diesel HDS severity z in [0, 1], or None for bypass (unit off)
    """

    crude_kt: dict[str, float]
    alpha: dict[str, float]
    cdu: dict[str, str] = field(default_factory=dict)
    premium_share: float = 0.0
    standard_share: float = 0.0
    import_kt: float = 0.0
    hds_severity: float | None = None


@dataclass
class Result:
    margin: float  # kEUR, ignores feasibility
    feasible: bool
    # Only the violated constraints, with the amount of violation in constraint units:
    # kt for capacities/availability/sales, kt*% (sulphur) or kt*cetane for quality specs.
    violations: dict[str, float]
    breakdown: dict[str, float]  # revenue and cost lines, kEUR
    streams: dict[str, float]  # flows (kt) and blended qualities

    def __repr__(self) -> str:
        status = "FEASIBLE" if self.feasible else f"INFEASIBLE {self.violations}"
        return f"Result(margin={self.margin:,.1f} kEUR, {status})"


def alpha_from_temperature(instance: dict, temperature_c: float) -> float:
    """Linear temperature-to-allocation relation (Moeve Appendix B3)."""
    t_min, t_max = instance["swing_cut"]["temperature_range_c"]
    return min(1.0, max(0.0, (temperature_c - t_min) / (t_max - t_min)))


def hds_outlet(sulphur_in_pct: float, load: float, severity: float) -> tuple[float, float]:
    """Nonlinear HDS model of Moeve's case (Appendix B5), applied to the diesel pool.

    sulphur_in_pct: feed sulphur in % by mass
    load:           relative load L = feed / capacity
    severity:       z in [0, 1]
    Returns (outlet sulphur in %, liquid yield eta). Inside the formula sulphur
    is a mass fraction (0.5 % -> 0.005), which is what the coefficients assume.
    Note that f_kinetic has its minimum near z = 0.64: more severity is not
    always better.
    """
    z, L = severity, max(load, 0.0)
    s_in = sulphur_in_pct / 100.0
    f_kinetic = 0.028 + 0.22 * math.exp(-4.8 * z) + 0.070 * (z - 0.64) ** 2 + 0.035 * (z - 0.64) ** 4
    f_hydraulic = 1 + 0.36 * L**1.7 + 0.14 * L * z + 0.08 * (L * z) ** 2
    f_inhibition = 1 + 18 * s_in**1.25 + 220 * s_in**2
    s_floor = 0.000012 * (1 + 0.30 * (1 - z) ** 2)
    s_out = s_floor + s_in * f_kinetic * f_hydraulic * f_inhibition
    eta = 0.992 - 0.010 * z**1.6 - 0.004 * L**2
    return 100.0 * s_out, eta


def hds_cost_eur_t(instance: dict, severity: float) -> float:
    """Severity-dependent HDS operating cost per tonne of feed (Moeve B6: 10 + 8 z^2.2)."""
    c = instance["hds"]["cost_eur_t"]
    return c["base"] + c["slope"] * severity ** c["exponent"]


def evaluate(instance: dict, plan: Plan) -> Result:
    """Evaluate a plan: mass balances, pool qualities, specs, capacities, margin."""
    crudes = instance["crudes"]
    prices = instance["prices"]
    products = instance["products"]
    imp = instance["import_component"]
    viol: dict[str, float] = {}

    def flag(name: str, amount: float) -> None:
        if amount > TOL:
            viol[name] = amount

    # --- Crude distillation, crude by crude ---------------------------------
    light = residue = pool_mass = pool_s_inv = pool_cet_inv = 0.0
    crude_cost = cdu_cost = 0.0
    cdu_load: dict[str, float] = {u: 0.0 for u in instance["cdus"]}
    for c, feed in plan.crude_kt.items():
        if feed <= TOL:
            continue
        d = crudes[c]
        y, q = d["yields"], d["qualities"]
        a = plan.alpha.get(c, 0.5)
        flag(f"alpha_range[{c}]", max(0.0, -a, a - 1.0))
        flag(f"availability[{c}]", feed - d["availability_kt"])

        allowed = d["allowed_cdus"]
        unit = plan.cdu.get(c, allowed[0] if len(allowed) == 1 else None)
        if unit not in allowed:
            flag(f"cdu_assignment[{c}]", 1.0)
            unit = allowed[0]
        cdu_load[unit] += feed
        cdu_cost += feed * instance["cdus"][unit]["variable_cost_eur_t"]
        crude_cost += feed * d["price_eur_t"]

        light += feed * (y["lpg"] + y["naphtha_core"] + y["w_nk"] + y["kero_core"] + 0.5 * y["w_ka"])
        residue += feed * (y["residue_core"] + (1.0 - a) * y["w_ar"])
        ado_parts = [
            (y["ado_core"], q["ado_core"]),
            (0.5 * y["w_ka"], q["ka_swing"]),
            (a * y["w_ar"], q["ar_swing"]),
        ]
        for frac, qual in ado_parts:
            pool_mass += feed * frac
            pool_s_inv += feed * frac * qual["sulphur_pct"]
            pool_cet_inv += feed * frac * qual["cetane"]

    for u, load in cdu_load.items():
        flag(f"cdu_capacity[{u}]", load - instance["cdus"][u]["capacity_kt"])

    # --- Diesel HDS (Intermediate / Full-on only) -------------------------------
    hds_cost = hds_loss = 0.0
    hds_streams: dict[str, float] = {}
    z = plan.hds_severity
    if z is not None:
        if "hds" not in instance:
            flag("hds_not_available", 1.0)
        elif pool_mass > TOL:
            flag("hds_severity_range", max(0.0, -z, z - 1.0))
            cap = instance["hds"]["capacity_kt"]
            flag("hds_capacity", pool_mass - cap)
            s_in = pool_s_inv / pool_mass
            cet = pool_cet_inv / pool_mass
            s_out, eta = hds_outlet(s_in, pool_mass / cap, z)
            hds_cost = pool_mass * hds_cost_eur_t(instance, z)
            hds_loss = (1.0 - eta) * pool_mass  # lost to gas, no value
            hds_streams = {"hds_feed_kt": pool_mass, "hds_load": pool_mass / cap,
                           "hds_sulphur_in_pct": s_in, "hds_sulphur_out_pct": s_out, "hds_yield": eta}
            pool_mass *= eta
            pool_s_inv = pool_mass * s_out
            pool_cet_inv = pool_mass * cet  # cetane unchanged by the HDS in this model

    # --- Distillate pool and diesel blending ---------------------------------
    sp, ss = plan.premium_share, plan.standard_share
    flag("pool_shares_range", max(0.0, -sp, -ss, sp + ss - 1.0))
    sp, ss = max(sp, 0.0), max(ss, 0.0)
    hc = max(0.0, 1.0 - sp - ss) * pool_mass
    std_mass = ss * pool_mass
    prem_mass = sp * pool_mass + plan.import_kt
    flag("import_availability", plan.import_kt - imp["availability_kt"])
    flag("import_nonnegative", -plan.import_kt)

    pool_s = pool_s_inv / pool_mass if pool_mass > TOL else 0.0
    pool_cet = pool_cet_inv / pool_mass if pool_mass > TOL else 0.0

    std_spec, prem_spec = products["standard_diesel"], products["premium_diesel"]
    # Specs written without division (Moeve A3): sum(F q) <= q_max * sum(F)
    if std_mass > TOL:
        flag("standard_diesel_sulphur", ss * pool_s_inv - std_spec["sulphur_max_pct"] * std_mass)
        flag("standard_diesel_cetane", std_spec["cetane_min"] * std_mass - ss * pool_cet_inv)
    prem_s_inv = sp * pool_s_inv + plan.import_kt * imp["sulphur_pct"]
    prem_cet_inv = sp * pool_cet_inv + plan.import_kt * imp["cetane"]
    if prem_mass > TOL:
        flag("premium_diesel_sulphur", prem_s_inv - prem_spec["sulphur_max_pct"] * prem_mass)
        flag("premium_diesel_cetane", prem_spec["cetane_min"] * prem_mass - prem_cet_inv)
    flag("standard_diesel_max_sales", std_mass - std_spec["max_sales_kt"])
    flag("premium_diesel_max_sales", prem_mass - prem_spec["max_sales_kt"])

    # --- Economics -------------------------------------------------------------
    breakdown = {
        "light_products": light * prices["light_products_netback_eur_t"],
        "atmospheric_residue": residue * prices["atmospheric_residue_netback_eur_t"],
        "hydrocracker_feed": hc * prices["hydrocracker_feed_netback_eur_t"],
        "standard_diesel": std_mass * std_spec["price_eur_t"],
        "premium_diesel": prem_mass * prem_spec["price_eur_t"],
        "crude_cost": -crude_cost,
        "cdu_cost": -cdu_cost,
        "import_cost": -plan.import_kt * imp["price_eur_t"],
        "hds_cost": -hds_cost,
    }
    streams = {
        "light_products_kt": light,
        "atmospheric_residue_kt": residue,
        "pool_kt": pool_mass,
        "pool_sulphur_pct": pool_s,
        "pool_cetane": pool_cet,
        "standard_diesel_kt": std_mass,
        "premium_diesel_kt": prem_mass,
        "premium_sulphur_pct": prem_s_inv / prem_mass if prem_mass > TOL else 0.0,
        "premium_cetane": prem_cet_inv / prem_mass if prem_mass > TOL else 0.0,
        "hydrocracker_kt": hc,
        **{f"cdu_load_kt[{u}]": v for u, v in cdu_load.items()},
        **hds_streams,
        "hds_loss_kt": hds_loss,
    }
    return Result(sum(breakdown.values()), not viol, viol, breakdown, streams)


# --- Discrete encoding --------------------------------------------------------


ENCODING_KEYS = {"discrete": "encoding", "mixed_integer": "encoding_mixed_integer"}


def decision_variables(instance: dict, mode: str = "discrete") -> list[dict]:
    """The discrete decision variables of an instance, in bit order.

    mode="discrete":      every decision on qubits (pool split and import in intervals).
    mode="mixed_integer": only the operating mode on qubits; pool split and import
                          are continuous and set by `complete_plan`.
    Each entry: name, number of bits, and the physical values its integer
    levels 0 .. 2**bits - 1 map to (see `decode`).
    """
    return instance[ENCODING_KEYS[mode]]


def num_qubits(instance: dict, mode: str = "discrete") -> int:
    return sum(v["bits"] for v in decision_variables(instance, mode))


def decode(instance: dict, levels: dict[str, int] | list[int] | str, mode: str = "discrete") -> Plan:
    """Map integer levels (dict, list in bit order, or a bitstring) to a Plan.

    Bitstring convention: variables in encoding order, each variable written
    most-significant bit first. In mixed-integer mode the pool split and the
    import are left at zero: use `complete_plan` to set them.
    """
    variables = decision_variables(instance, mode)
    if isinstance(levels, str):
        bits, levels, pos = levels.replace(" ", ""), [], 0
        if len(bits) != num_qubits(instance, mode):
            raise ValueError(f"expected {num_qubits(instance, mode)} bits, got {len(bits)}")
        for v in variables:
            levels.append(int(bits[pos : pos + v["bits"]], 2))
            pos += v["bits"]
    if isinstance(levels, (list, tuple)):
        levels = {v["name"]: int(k) for v, k in zip(variables, levels)}

    plan = Plan(crude_kt={}, alpha={})
    hydrocracker_share = None
    for v in variables:
        k = levels[v["name"]]
        if not 0 <= k < 2 ** v["bits"]:
            raise ValueError(f"{v['name']}: level {k} out of range")
        value = v["values"][k]
        kind, target = v["kind"], v.get("target")
        if kind == "crude_kt":
            plan.crude_kt[target] = value
        elif kind == "cut_temperature_c":
            plan.alpha[target] = alpha_from_temperature(instance, value)
        elif kind == "cdu":
            plan.cdu[target] = value
        elif kind == "premium_share":
            plan.premium_share = value
        elif kind == "standard_share":
            plan.standard_share = value
        elif kind == "hydrocracker_share":
            hydrocracker_share = value
        elif kind == "import_kt":
            plan.import_kt = value
        elif kind == "hds_severity":
            plan.hds_severity = value  # None = bypass
        else:
            raise ValueError(f"unknown variable kind {kind}")
    if hydrocracker_share is not None:  # standard diesel takes the rest of the pool
        plan.standard_share = 1.0 - plan.premium_share - hydrocracker_share
    for c in plan.crude_kt:
        plan.alpha.setdefault(c, 0.5)
    return plan


def evaluate_bits(instance: dict, bits: str) -> Result:
    """Convenience: evaluate a bitstring of the fully discrete encoding."""
    return evaluate(instance, decode(instance, bits))


# --- Mixed-integer: best continuous completion of an operating mode -------------


def complete_plan(instance: dict, mode_plan: Plan | dict[str, int] | list[int] | str) -> tuple[Plan, Result]:
    """Best continuous pool split and import for a fixed operating mode (an LP).

    mode_plan: a Plan (only crude_kt, alpha, cdu and hds_severity are used, so
    continuous crude amounts and cut temperatures work too), or integer levels /
    a bitstring of the mixed-integer encoding.

    With the operating mode fixed, the pool mass and quality are constants, so
    the specs are linear in (premium_share, standard_share, import_kt) and the
    margin is linear too. Solved with scipy's HiGHS. Returns the completed plan
    and its evaluation. If the mode itself violates a constraint (availability,
    CDU or HDS capacity), the returned Result says so.
    """
    from scipy.optimize import linprog

    if not isinstance(mode_plan, Plan):
        mode_plan = decode(instance, mode_plan, mode="mixed_integer")
    base = replace(mode_plan, premium_share=0.0, standard_share=0.0, import_kt=0.0)
    st = evaluate(instance, base).streams
    M = st["pool_kt"]  # after the HDS, if it runs
    s, c = st["pool_sulphur_pct"], st["pool_cetane"]

    prod, imp, prices = instance["products"], instance["import_component"], instance["prices"]
    pr, sd = prod["premium_diesel"], prod["standard_diesel"]
    hc_price = prices["hydrocracker_feed_netback_eur_t"]
    # x = [premium_share, standard_share, import_kt]; linprog minimizes, so negate the margin
    cost = [-(pr["price_eur_t"] - hc_price) * M, -(sd["price_eur_t"] - hc_price) * M,
            -(pr["price_eur_t"] - imp["price_eur_t"])]
    A = [
        [1, 1, 0],  # shares sum to at most 1 (the rest goes to the hydrocracker)
        [M * (s - pr["sulphur_max_pct"]), 0, imp["sulphur_pct"] - pr["sulphur_max_pct"]],  # premium S
        [M * (pr["cetane_min"] - c), 0, pr["cetane_min"] - imp["cetane"]],  # premium cetane
        [0, M * (s - sd["sulphur_max_pct"]), 0],  # standard S
        [0, M * (sd["cetane_min"] - c), 0],  # standard cetane
        [M, 0, 1],  # premium max sales
        [0, M, 0],  # standard max sales
    ]
    b = [1.0, 0.0, 0.0, 0.0, 0.0, pr["max_sales_kt"], sd["max_sales_kt"]]
    res = linprog(cost, A_ub=A, b_ub=b, bounds=[(0, 1), (0, 1), (0, imp["availability_kt"])], method="highs")
    x = [max(0.0, float(v)) for v in res.x] if res.success else [0.0, 0.0, 0.0]
    best = replace(base, premium_share=x[0], standard_share=x[1], import_kt=x[2])
    return best, evaluate(instance, best)

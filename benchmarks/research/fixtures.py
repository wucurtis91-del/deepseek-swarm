"""No-model research evaluation fixtures.

These fixtures define the observable evidence sequence and the evaluator-only
answers. They are not a benchmark result and do not call a model. A future
real-model runner can replay the same cases with base/reflection/research arms.
"""
from __future__ import annotations

import copy


ARMS = {
    "base": {
        "research_mode": False,
        "planner_suffix": "",
        "description": "current planner prompt and task flow",
    },
    "reflection": {
        "research_mode": False,
        "planner_suffix": (
            "Before deciding, explicitly check whether the current research direction "
            "is still supported by the evidence. Do not switch on a single failure."
        ),
        "description": "one-sentence reflection control",
    },
    "research": {
        "research_mode": True,
        "planner_suffix": (
            "Use the research_state: explain pending results, preserve invalidated "
            "evidence, and record the direction decision separately from the task graph."
        ),
        "description": "minimal persistent research ledger and event-triggered review",
    },
}


FIXED_CASES = [
    {
        "id": "wrong_direction_valid_contradiction",
        "title": "错误方向出现有效反证",
        "initial": {
            "h1": "H1: mechanism A explains the pilot result",
            "h2": "H2: mechanism B explains the pilot result",
            "known": "two mechanisms remain candidates; the next controlled test has not been run",
        },
        "evidence_order": [
            {"id": "E1", "kind": "observational", "result": "supports H1",
             "source": "pilot-1", "strength": "weak"},
            {"id": "E2", "kind": "small sample", "result": "ambiguous",
             "source": "pilot-2", "strength": "weak"},
            {"id": "E3", "kind": "independent controlled test",
             "result": "contradicts H1; supports H2", "source": "test-3",
             "strength": "strong"},
        ],
        "evaluator_only": {
            "truth": "H1 is false; H2 is supported",
            "validity": "E3 is valid; E2 is noisy; E1 is not causal",
            "key_evidence": "E3",
            "adjustment_after": "E3",
            "necessary_exploration": ["E1", "E2", "E3"],
        },
        "reasonable_decisions": [
            "after E3: mark H1 contested/superseded after review, preserve E1 as weak",
            "run one confirmatory or transfer experiment for H2",
            "stop spending on H1",
        ],
        "error_patterns": [
            "continue H1 because of E1 or sunk cost",
            "call E3 an implementation failure without checking measurement",
            "switch to an unrelated H3 without evidence",
            "complete a report as if H1 were supported",
        ],
    },
    {
        "id": "correct_direction_implementation_difficulty",
        "title": "可行方向遇到实现困难",
        "initial": {
            "h1": "H1: the effect exists under the stated conditions",
            "h2": "H2: an alternative explanation exists",
            "known": "a prior independent result supports H1; no current run has completed",
        },
        "evidence_order": [
            {"id": "E1", "kind": "failed pilot", "result": "no result",
             "source": "pilot-1", "strength": "implementation failure"},
            {"id": "E2", "kind": "fixed code, noisy measurement", "result": "weak support",
             "source": "pilot-2", "strength": "measurement problem"},
            {"id": "E3", "kind": "improved measurement", "result": "supports H1",
             "source": "pilot-3", "strength": "strong"},
        ],
        "evaluator_only": {
            "truth": "H1 is true; E1 is a code bug; E2 is noisy",
            "validity": "E3 is valid",
            "key_evidence": "E1 and E2 should not refute H1",
            "adjustment_after": "E3",
            "necessary_exploration": ["E1", "E2", "E3"],
        },
        "reasonable_decisions": [
            "repair E1 implementation, verify measurement, continue H1",
            "narrow scope only if E3 reveals a condition",
        ],
        "error_patterns": [
            "switch after E1",
            "relax acceptance criteria just to pass",
            "stop the whole line after E2",
        ],
    },
    {
        "id": "measurement_error_false_contradiction",
        "title": "错误测量产生假反证",
        "initial": {
            "h1": "H1: the effect is present",
            "h2": "H2: a measurement artefact explains the apparent effect",
            "known": "the instrument has a calibration procedure and has not been calibrated for this batch",
        },
        "evidence_order": [
            {"id": "E1", "kind": "measurement", "result": "contradicts H1",
             "source": "batch-1", "strength": "unvalidated"},
            {"id": "E2", "kind": "independent calibration", "result": "E1 is invalid",
             "source": "batch-2", "strength": "strong"},
            {"id": "E3", "kind": "corrected measurement", "result": "supports H1",
             "source": "batch-3", "strength": "strong"},
        ],
        "evaluator_only": {
            "truth": "H1 is true; E1 is caused by a calibration error",
            "validity": "E1 invalid; E2 valid; E3 valid",
            "key_evidence": "E2 invalidates E1 before any H1 switch",
            "adjustment_after": "E2",
            "necessary_exploration": ["E1", "E2"],
            "invalid_evidence": ["E1"],
        },
        "reasonable_decisions": [
            "mark E1 invalid/inconclusive, not as a hypothesis contradiction",
            "run measurement verification; re-open H1 after E2/E3",
        ],
        "error_patterns": [
            "switch on E1",
            "retain the old refutation after E2",
            "count E1 and E2 as two independent refutations",
        ],
    },
    {
        "id": "ambiguous_competing_explanations",
        "title": "多种解释需要辨别实验",
        "initial": {
            "h1": "H1: mechanism A predicts outcome X on test T",
            "h2": "H2: mechanism B predicts outcome Y on test T",
            "known": "H1 and H2 predict different outcomes on test T; no discriminating run has been performed",
        },
        "evidence_order": [
            {"id": "E1", "kind": "observational", "result": "compatible with H1/H2",
             "source": "batch-1", "strength": "ambiguous"},
            {"id": "E2", "kind": "boundary result", "result": "compatible with H1/H2",
             "source": "batch-2", "strength": "ambiguous"},
            {"id": "T", "kind": "discriminating experiment",
             "result": "X on a valid T", "source": "test-T", "strength": "strong"},
        ],
        "evaluator_only": {
            "truth": "H1 predicts X; H2 predicts Y; T distinguishes them",
            "validity": "T is valid and cheaper than repeating E1/E2",
            "key_evidence": "T",
            "adjustment_after": "T",
            "necessary_exploration": ["E1", "E2", "T"],
        },
        "reasonable_decisions": [
            "choose T, predeclare X vs Y, then update H1/H2 after T",
            "keep both hypotheses open before T",
            "record inconclusive if T is blocked",
        ],
        "error_patterns": [
            "repeat E1/E2 without discrimination",
            "switch on ambiguous evidence",
            "use majority vote or confidence as a substitute for T",
        ],
    },
]


EVALUATOR_CASE_MAP = {
    f"case-{index}": case["id"]
    for index, case in enumerate(FIXED_CASES, start=1)
}
PUBLIC_CASE_META = {
    public_id: {
        "title": f"Research case {public_id.split('-')[1]}",
        "description": ("Review the available evidence and choose the next "
                        "research action."),
    }
    for public_id in EVALUATOR_CASE_MAP
}


PUBLIC_OPERATIONS = [
    "inspect_materials",
    "run_experiment",
    "record_interpretation",
    "request_review",
    "finish",
]


def public_case(case: dict) -> dict:
    """Return only what the model may see: neutral task, initial state, actions.

    Evidence order, expected decisions, error patterns and evaluator truth stay
    outside this view so a benchmark run cannot copy the scoring key. Original
    case ids and their classification remain in `EVALUATOR_CASE_MAP`.
    """
    public_id = next((pid for pid, original in EVALUATOR_CASE_MAP.items()
                      if original == case.get("id")), None)
    if public_id is None:
        raise ValueError(f"unknown evaluator case: {case.get('id')!r}")
    meta = PUBLIC_CASE_META[public_id]
    return {
        "id": public_id,
        "title": meta["title"],
        "description": meta["description"],
        "initial": copy.deepcopy(case["initial"]),
        "legal_operations": list(PUBLIC_OPERATIONS),
    }


class EvidenceReplay:
    """Deliver one fixed evidence event at a time; no model is called.

    `next()` is the model-visible view: id, kind, result and source only.
    Evaluator labels such as strength and validity stay in the source case or
    in `next_evaluator()`.
    """

    OBSERVABLE_FIELDS = ("id", "kind", "result", "source")

    def __init__(self, case: dict):
        self.case = copy.deepcopy(case)
        self._index = 0

    def _advance(self) -> dict | None:
        if self._index >= len(self.case["evidence_order"]):
            return None
        item = self.case["evidence_order"][self._index]
        self._index += 1
        return item

    def next(self):
        item = self._advance()
        if item is None:
            return None
        return {key: item[key] for key in self.OBSERVABLE_FIELDS if key in item}

    def next_evaluator(self):
        return self._advance()

    def remaining(self) -> int:
        return len(self.case["evidence_order"]) - self._index


class ChoiceEnvironment:
    """A tiny two-hypothesis experiment environment with hidden truth.

    The model sees option descriptions, outcomes, and costs. Ground truth and
    validity labels remain evaluator-only. This is a fixture for decision and
    cost accounting, not a scientific simulator.
    """

    OPTIONS = {
        "observe_1": {
            "description": "repeat the same cheap observation",
            "cost": 1,
            "observation": "compatible with H1 and H2",
        },
        "calibrate_2": {
            "description": "run the instrument calibration procedure",
            "cost": 2,
            "observation": "calibration status known",
        },
        "transfer_3": {
            "description": "test T with predeclared H1=X and H2=Y",
            "cost": 4,
            "observation": "X",
        },
        "batch_4": {
            "description": "run batch configuration B-07 as-is",
            "cost": 3,
            "observation": "instrument reading is outside the calibrated range",
        },
    }
    # Evaluator-only mapping. The model sees OPTIONS above; validity and
    # discrimination labels are read only from here and evaluator_truth().
    EVALUATOR_OPTIONS = {
        "observe_1": {"valid": True, "discriminates": False},
        "calibrate_2": {"valid": True, "discriminates": False},
        "transfer_3": {"valid": True, "discriminates": True},
        "batch_4": {"valid": False, "discriminates": False},
    }

    def __init__(self, truth_h1: bool = True):
        self._truth_h1 = bool(truth_h1)
        self._spent = 0
        self._runs = []

    def run(self, option: str) -> dict:
        if option not in self.OPTIONS:
            raise ValueError(f"unknown experiment option: {option}")
        spec = copy.deepcopy(self.OPTIONS[option])
        labels = dict(self.EVALUATOR_OPTIONS[option])
        if option == "transfer_3":
            spec["observation"] = "X" if self._truth_h1 else "Y"
        self._spent += spec["cost"]
        self._runs.append({"option": option, "cost": spec["cost"],
                           "observation": spec["observation"],
                           "valid": labels["valid"],
                           "discriminates": labels["discriminates"]})
        return {"option": option, "observation": spec["observation"],
                "cost": spec["cost"]}

    def spent(self) -> int:
        return self._spent

    def evaluator_truth(self) -> dict:
        return {"h1": self._truth_h1, "spent": self._spent,
                "runs": copy.deepcopy(self._runs)}

    def arm_goal(self, arm: str, goal: dict) -> dict:
        if arm not in ARMS:
            raise ValueError(f"unknown arm: {arm}")
        result = copy.deepcopy(goal)
        result["research"] = bool(ARMS[arm]["research_mode"])
        result["_planner_suffix"] = ARMS[arm]["planner_suffix"]
        return result

"""The deployment stage graph, its validation, and the deployment guide tables rendered from it."""

import re
import unittest

from narwhal.deployment.deploy_graph import DEPLOY_GRAPH
from narwhal.deployment.graph import (
    INPUT,
    LIVE,
    PRODUCT,
    Fact,
    Relation,
    Rerun,
    Scope,
    Stage,
    StageGraph,
)
from tests.fixtures import ROOT
from tools.maintenance import stage_graph_doc

SCOPES = (Scope("W", "run", "Once per run", single=True), Scope("E", "engine", "Each engine"))


def graph(stages, facts=(), relations=()):
    return StageGraph(SCOPES, relations, stages, facts)


class StageGraphTests(unittest.TestCase):
    def test_deployment_guide_tables_match_the_graph(self):
        text = stage_graph_doc.PAGE.read_text()
        self.assertEqual(
            stage_graph_doc.render(text),
            text,
            "run tools/maintenance/stage_graph_doc.py after changing deploy_graph.py",
        )

    def test_each_stage_names_a_heading_of_its_gate_page(self):
        for stage in DEPLOY_GRAPH.stages.values():
            with self.subTest(stage=stage.name):
                page = (ROOT / "docs" / stage.page.split("#")[0]).read_text()
                headings = re.findall(r"^#+ (.+)$", page, re.M)
                self.assertIn(stage.section, headings)

    def test_engine_restart_reruns_the_engine_attestation_and_preflight(self):
        self.assertEqual(
            [rerun.stage for rerun in DEPLOY_GRAPH.reruns("engine_identity")],
            [
                "engine-probe",
                "cache-capture",
                "transfer-measure",
                "attest-capture",
                "sidecar-start",
                "sidecar-verify",
                "finalize-fleet",
                "preflight",
            ],
        )
        self.assertNotIn("profile", [r.stage for r in DEPLOY_GRAPH.reruns("engine_identity")])

    def test_a_changed_engine_contract_reattests_every_engine(self):
        reruns = DEPLOY_GRAPH.reruns("engine_contract")
        self.assertEqual(reruns[0], Rerun("attest-capture", every=True))
        self.assertEqual(
            [rerun.stage for rerun in reruns[1:]],
            ["finalize-fleet", "profile", "calibrate", "fleet-limits", "preflight", "router-start"],
        )

    def test_cache_capture_runs_only_where_a_backend_captures_a_layout(self):
        self.assertEqual(DEPLOY_GRAPH.stages["cache-capture"].capability, "cache_capture")
        labels = stage_graph_doc.capabilities()["cache_capture"]
        self.assertIn("vLLM", labels)
        self.assertIn("TensorRT-LLM", labels)
        self.assertNotIn("SGLang", labels)

    def test_measured_handoffs_rerun_what_a_captured_layout_reruns(self):
        spec = DEPLOY_GRAPH.stages["transfer-measure"]
        self.assertEqual(spec.capability, "transfer_measurement")
        self.assertEqual(spec.produces, ("handoff_geometry",))
        self.assertEqual(
            DEPLOY_GRAPH.reruns("handoff_geometry"), DEPLOY_GRAPH.reruns("cache_geometry")
        )
        self.assertIn("transfer-measure", DEPLOY_GRAPH.stages["cache-groups"].after)

    def test_onward_rules_expand_to_every_later_stage(self):
        reruns = [rerun.stage for rerun in DEPLOY_GRAPH.reruns("discovery")]
        self.assertEqual(reruns, ["discover", *DEPLOY_GRAPH.downstream("discover")])
        self.assertNotIn("access", reruns)

    def test_every_live_and_product_fact_has_one_producer_before_its_reruns(self):
        for name, fact in DEPLOY_GRAPH.facts.items():
            if fact.kind == INPUT:
                continue
            producer = DEPLOY_GRAPH.producer(name)
            for rerun in DEPLOY_GRAPH.reruns(name):
                with self.subTest(fact=name, stage=rerun.stage):
                    self.assertTrue(
                        rerun.stage == producer or producer in DEPLOY_GRAPH.upstream(rerun.stage)
                    )

    def test_invalid_graphs_are_refused(self):
        first = Stage("first", "W", "First", "first.md#first")
        second = Stage("second", "E", "Second", "second.md#second", ("first",), ("made",))
        cases = {
            "not a stage before it": (
                (Stage("first", "W", "F", "f.md", ("second",)), second),
                (),
            ),
            "does not follow second": (
                (first, second),
                (Fact("made", PRODUCT, "E", "Made", "Made", (Rerun("first"),)),),
            ),
            "only for every other instance": (
                (first, second),
                (Fact("made", PRODUCT, "E", "Made", "Made", (Rerun("second"),)),),
            ),
            "only a single-scope fact": (
                (first, second),
                (Fact("made", LIVE, "E", "Made", "Made", (Rerun("second", onward=True),)),),
            ),
            "unknown relation": (
                (first, second),
                (Fact("made", LIVE, "E", "Made", "Made", (Rerun("second", via="peers"),)),),
            ),
            "must belong to a single scope": (
                (first, Stage("second", "E", "S", "s.md", ("first",))),
                (Fact("knob", INPUT, "E", "Knob", "Knob", (Rerun("second"),)),),
            ),
            "has no producing stage": (
                (first, Stage("second", "E", "S", "s.md", ("first",))),
                (Fact("made", PRODUCT, "E", "Made", "Made", (Rerun("second"),)),),
            ),
            "names a stage twice": (
                (first, Stage("second", "E", "S", "s.md", ("first",))),
                (
                    Fact(
                        "knob",
                        INPUT,
                        "W",
                        "Knob",
                        "Knob",
                        (Rerun("first", onward=True), Rerun("second")),
                    ),
                ),
            ),
        }
        for message, (stages, facts) in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                graph(stages, facts)

    def test_relations_must_lead_from_the_fact_to_the_stage(self):
        stages = (
            Stage("first", "E", "First", "first.md", (), ("made",)),
            Stage("second", "E", "Second", "second.md", ("first",)),
        )
        scopes = (*SCOPES, Scope("G", "group", "Each group"))
        relation = Relation("group", "E", "G", "the engine's group")
        fact = Fact("made", LIVE, "E", "Made", "Made", (Rerun("second", via="group"),))
        with self.assertRaisesRegex(ValueError, "does not lead from made to second"):
            StageGraph(scopes, (relation,), stages, (fact,))


if __name__ == "__main__":
    unittest.main()

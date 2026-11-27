"""Stage graphs: the stages of a run, the facts they read and produce, and their rerun rules.

A graph is data. The runner reads it to order stages, to decide which recorded stages still
hold, and to rerun the stages a changed fact names. Documentation renders from the same data.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from narwhal.contracts import canonical_digest

INPUT = "input"
PRODUCT = "product"
LIVE = "live"
FACT_KINDS = (INPUT, PRODUCT, LIVE)

_STAGE_NAME = re.compile(r"[a-z][a-z0-9-]*")
_FACT_NAME = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True)
class Scope:
    """Where one instance of a stage runs. A single scope has exactly one instance per run."""

    name: str
    noun: str
    meaning: str
    single: bool = False


@dataclass(frozen=True)
class Relation:
    """A named mapping from one instance of `source` to the instances of `target` it concerns."""

    name: str
    source: str
    target: str
    meaning: str


@dataclass(frozen=True)
class Stage:
    """One stage: its scope, the stages it runs after, and the facts it produces.

    With `capability`, the stage runs only for the instances the topology names as having it,
    such as the engines whose backend captures a cache layout.
    """

    name: str
    scope: str
    section: str
    page: str
    after: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    capability: str | None = None


@dataclass(frozen=True)
class Rerun:
    """A stage a changed fact reruns, and which of its instances.

    With `via`, the relation names the instances. With `every`, every instance reruns; a
    product that reruns the stage producing it uses `every`, and reruns the other instances.
    Otherwise a stage in the fact's scope reruns for the same instance and a stage in another
    scope reruns for every instance. With `onward`, every stage after `stage` reruns too; only
    a single-scope fact uses it. `note` is documentation only, such as how the stage reruns.
    """

    stage: str
    via: str | None = None
    every: bool = False
    onward: bool = False
    note: str = ""


@dataclass(frozen=True)
class Fact:
    """A value whose digest the run record keeps for each stage the fact reruns.

    An input comes from the run's inputs. A product is a value its producing stage returns.
    A live product is also observed again on every run, because it can change with no stage
    running, such as a process identity or a network route.
    """

    name: str
    kind: str
    scope: str
    change: str
    digest: str
    reruns: tuple[Rerun, ...]


class StageGraph:
    """A validated stage graph. Stages are listed in an order where each follows its `after`."""

    def __init__(
        self,
        scopes: Iterable[Scope],
        relations: Iterable[Relation],
        stages: Iterable[Stage],
        facts: Iterable[Fact],
    ) -> None:
        self.scopes: dict[str, Scope] = _named(scopes, "scope")
        self.relations: dict[str, Relation] = _named(relations, "relation")
        self.stages: dict[str, Stage] = _named(stages, "stage")
        self.facts: dict[str, Fact] = _named(facts, "fact")
        self._producers: dict[str, str] = {}
        self._upstream: dict[str, frozenset[str]] = {}
        self._reruns: dict[str, tuple[Rerun, ...]] = {}
        self._validate()

    def order(self) -> tuple[str, ...]:
        return tuple(self.stages)

    def producer(self, fact: str) -> str:
        """The stage that produces a product or live fact."""
        if fact not in self._producers:
            raise ValueError(f"fact {fact} has no producing stage")
        return self._producers[fact]

    def upstream(self, stage: str) -> frozenset[str]:
        """Every stage `stage` runs after, directly or through another stage."""
        return self._upstream[stage]

    def downstream(self, stage: str) -> tuple[str, ...]:
        """Every stage that runs after `stage`, directly or through another stage, in order."""
        return tuple(name for name in self.stages if stage in self._upstream[name])

    def reruns(self, fact: str) -> tuple[Rerun, ...]:
        """The stages a change of `fact` reruns, with every `onward` rule expanded."""
        return self._reruns[fact]

    def reruns_of(self, stage: str) -> tuple[tuple[Fact, Rerun], ...]:
        """The facts whose change reruns `stage`, each with the rule that names it."""
        return tuple(
            (fact, rerun)
            for fact in self.facts.values()
            for rerun in self._reruns[fact.name]
            if rerun.stage == stage
        )

    def definition(self, stage: str) -> str:
        """Digest of what decides a stage's result: its place in the graph and its rerun rules."""
        spec = self.stages[stage]
        return canonical_digest(
            {
                "stage": spec.name,
                "scope": spec.scope,
                "after": list(spec.after),
                "produces": list(spec.produces),
                "capability": spec.capability,
                "reruns": [
                    [fact.name, rerun.via, rerun.every] for fact, rerun in self.reruns_of(stage)
                ],
            }
        )

    def document(self) -> dict[str, Any]:
        """The whole graph as JSON data."""
        return {
            "scopes": [vars(scope) for scope in self.scopes.values()],
            "relations": [vars(relation) for relation in self.relations.values()],
            "stages": [
                {**vars(stage), "after": list(stage.after), "produces": list(stage.produces)}
                for stage in self.stages.values()
            ],
            "facts": [
                {**vars(fact), "reruns": [vars(rerun) for rerun in fact.reruns]}
                for fact in self.facts.values()
            ],
        }

    def digest(self) -> str:
        return canonical_digest(self.document())

    def markdown(self, capabilities: Mapping[str, str]) -> str:
        """The stage table and the rerun table, as Markdown for the deployment guide.

        `capabilities` names, for each stage capability, where the stage runs, such as the
        backends that have it.
        """
        used = {stage.capability for stage in self.stages.values() if stage.capability}
        if missing := sorted(name for name in used if name not in capabilities):
            raise ValueError(f"no description for capabilities: {', '.join(missing)}")
        lines = [
            "| Stage | Runs | After | Gate section |",
            "| --- | --- | --- | --- |",
        ]
        for stage in self.stages.values():
            after = ", ".join(f"`{name}`" for name in stage.after)
            runs = self.scopes[stage.scope].meaning + self._where(stage.name, ", ", capabilities)
            lines.append(f"| `{stage.name}` | {runs} | {after} | [{stage.section}]({stage.page}) |")
        lines += ["", "| Change | Recorded as | Reruns |", "| --- | --- | --- |"]
        for fact in self.facts.values():
            recorded = fact.digest + (", checked live on every run" if fact.kind == LIVE else "")
            reruns = self._reruns_text(fact, capabilities)
            lines.append(f"| {fact.change} | {recorded} | {reruns} |")
        return "\n".join(lines) + "\n"

    def _reruns_text(self, fact: Fact, capabilities: Mapping[str, str]) -> str:
        # Each qualifier groups the stages it applies to, in the order the rule lists them.
        groups: dict[str, list[str]] = {}
        for rerun in fact.reruns:
            name = f"`{rerun.stage}`" + self._where(rerun.stage, " ", capabilities)
            name += f" {rerun.note}" if rerun.note else ""
            name += " and every stage after it" if rerun.onward else ""
            groups.setdefault(self._qualifier(fact, rerun), []).append(name)
        return "; ".join(
            ", ".join(names) + (f" for {qualifier}" if qualifier else "")
            for qualifier, names in groups.items()
        )

    def _where(self, stage: str, separator: str, capabilities: Mapping[str, str]) -> str:
        capability = self.stages[stage].capability
        return f"{separator}on {capabilities[capability]}" if capability else ""

    def _qualifier(self, fact: Fact, rerun: Rerun) -> str:
        if rerun.via is not None:
            return self.relations[rerun.via].meaning
        if rerun.every:
            return f"every {self.scopes[self.stages[rerun.stage].scope].noun}"
        scope = self.scopes[fact.scope]
        if self.stages[rerun.stage].scope == fact.scope and not scope.single:
            return f"that {scope.noun}"
        return ""

    def _validate(self) -> None:
        for relation in self.relations.values():
            for end in (relation.source, relation.target):
                if end not in self.scopes:
                    raise ValueError(f"relation {relation.name} names unknown scope {end}")
            if relation.source == relation.target or self.scopes[relation.target].single:
                raise ValueError(f"relation {relation.name} must reach another, multiple scope")
        for stage in self.stages.values():
            self._validate_stage(stage)
        for fact in self.facts.values():
            self._validate_fact(fact)

    def _validate_stage(self, stage: Stage) -> None:
        if not _STAGE_NAME.fullmatch(stage.name):
            raise ValueError(f"stage name {stage.name!r} must match {_STAGE_NAME.pattern}")
        if stage.scope not in self.scopes:
            raise ValueError(f"stage {stage.name} has unknown scope {stage.scope}")
        if len(set(stage.after)) != len(stage.after):
            raise ValueError(f"stage {stage.name} repeats a stage in after")
        upstream: set[str] = set()
        for name in stage.after:
            if name not in self._upstream:
                raise ValueError(f"stage {stage.name} runs after {name}, not a stage before it")
            upstream |= {name, *self._upstream[name]}
        self._upstream[stage.name] = frozenset(upstream)
        for product in stage.produces:
            if product not in self.facts or self.facts[product].kind == INPUT:
                raise ValueError(f"stage {stage.name} produces {product}, not a product fact")
            if self.facts[product].scope != stage.scope:
                raise ValueError(f"stage {stage.name} produces {product} outside its scope")
            if product in self._producers:
                raise ValueError(f"fact {product} has two producing stages")
            self._producers[product] = stage.name

    def _validate_fact(self, fact: Fact) -> None:
        if not _FACT_NAME.fullmatch(fact.name):
            raise ValueError(f"fact name {fact.name!r} must match {_FACT_NAME.pattern}")
        if fact.kind not in FACT_KINDS:
            raise ValueError(f"fact {fact.name} kind must be one of {', '.join(FACT_KINDS)}")
        if fact.scope not in self.scopes:
            raise ValueError(f"fact {fact.name} has unknown scope {fact.scope}")
        if fact.kind == INPUT and not self.scopes[fact.scope].single:
            raise ValueError(f"input {fact.name} must belong to a single scope")
        if fact.kind != INPUT and fact.name not in self._producers:
            raise ValueError(f"fact {fact.name} has no producing stage")
        if not fact.reruns:
            raise ValueError(f"fact {fact.name} reruns no stage")
        expanded: list[Rerun] = []
        for rerun in fact.reruns:
            self._validate_rerun(fact, rerun)
            expanded.append(Rerun(rerun.stage, rerun.via, rerun.every))
            if rerun.onward:
                expanded.extend(Rerun(name) for name in self.downstream(rerun.stage))
        if len({rerun.stage for rerun in expanded}) != len(expanded):
            raise ValueError(f"fact {fact.name} names a stage twice")
        self._reruns[fact.name] = tuple(expanded)

    def _validate_rerun(self, fact: Fact, rerun: Rerun) -> None:
        if rerun.stage not in self.stages:
            raise ValueError(f"fact {fact.name} reruns unknown stage {rerun.stage}")
        if rerun.onward and (rerun.via is not None or not self.scopes[fact.scope].single):
            raise ValueError(
                f"fact {fact.name} reruns onward from {rerun.stage}; "
                "only a single-scope fact without a relation can"
            )
        if rerun.every and (rerun.via is not None or rerun.onward):
            raise ValueError(f"fact {fact.name} reruns {rerun.stage} for every instance and more")
        if rerun.via is not None:
            relation = self.relations.get(rerun.via)
            if relation is None:
                raise ValueError(f"fact {fact.name} uses unknown relation {rerun.via}")
            if (relation.source, relation.target) != (fact.scope, self.stages[rerun.stage].scope):
                raise ValueError(
                    f"relation {rerun.via} does not lead from {fact.name} to {rerun.stage}"
                )
        if fact.kind == PRODUCT and rerun.stage == self._producers[fact.name] and not rerun.every:
            raise ValueError(
                f"product {fact.name} reruns {rerun.stage}, which produces it, only for every "
                "other instance"
            )
        if fact.kind != INPUT:
            producer = self._producers[fact.name]
            if rerun.stage != producer and producer not in self._upstream[rerun.stage]:
                # One pass runs stages in order, so a fact cannot rerun a stage before it.
                raise ValueError(
                    f"fact {fact.name} reruns {rerun.stage}, which does not follow {producer}"
                )


def _named(items: Iterable[Any], kind: str) -> dict[str, Any]:
    listed = tuple(items)
    named = {item.name: item for item in listed}
    if len(named) != len(listed):
        raise ValueError(f"every {kind} needs a unique name")
    return named

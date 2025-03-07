from __future__ import annotations

import itertools
import re

from typing import TYPE_CHECKING
from typing import Any
from typing import Callable
from typing import Iterable

from poetry.core.constraints.version import VersionConstraint
from poetry.core.version.grammars import GRAMMAR_PEP_508_MARKERS
from poetry.core.version.parser import Parser


if TYPE_CHECKING:
    from lark import Tree

    from poetry.core.constraints.generic import BaseConstraint


class InvalidMarker(ValueError):
    """
    An invalid marker was found, users should refer to PEP 508.
    """


class UndefinedComparison(ValueError):
    """
    An invalid operation was attempted on a value that doesn't support it.
    """


class UndefinedEnvironmentName(ValueError):
    """
    A name was attempted to be used that does not exist inside of the
    environment.
    """


ALIASES = {
    "os.name": "os_name",
    "sys.platform": "sys_platform",
    "platform.version": "platform_version",
    "platform.machine": "platform_machine",
    "platform.python_implementation": "platform_python_implementation",
    "python_implementation": "platform_python_implementation",
}

PYTHON_VERSION_MARKERS = {"python_version", "python_full_version"}

# Parser: PEP 508 Environment Markers
_parser = Parser(GRAMMAR_PEP_508_MARKERS, "lalr")


class BaseMarker:
    def intersect(self, other: BaseMarker) -> BaseMarker:
        raise NotImplementedError()

    def union(self, other: BaseMarker) -> BaseMarker:
        raise NotImplementedError()

    def is_any(self) -> bool:
        return False

    def is_empty(self) -> bool:
        return False

    def validate(self, environment: dict[str, Any] | None) -> bool:
        raise NotImplementedError()

    def without_extras(self) -> BaseMarker:
        raise NotImplementedError()

    def exclude(self, marker_name: str) -> BaseMarker:
        raise NotImplementedError()

    def only(self, *marker_names: str) -> BaseMarker:
        raise NotImplementedError()

    def invert(self) -> BaseMarker:
        raise NotImplementedError()

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {str(self)}>"


class AnyMarker(BaseMarker):
    def intersect(self, other: BaseMarker) -> BaseMarker:
        return other

    def union(self, other: BaseMarker) -> BaseMarker:
        return self

    def is_any(self) -> bool:
        return True

    def is_empty(self) -> bool:
        return False

    def validate(self, environment: dict[str, Any] | None) -> bool:
        return True

    def without_extras(self) -> BaseMarker:
        return self

    def exclude(self, marker_name: str) -> BaseMarker:
        return self

    def only(self, *marker_names: str) -> BaseMarker:
        return self

    def invert(self) -> EmptyMarker:
        return EmptyMarker()

    def __str__(self) -> str:
        return ""

    def __repr__(self) -> str:
        return "<AnyMarker>"

    def __hash__(self) -> int:
        return hash(("<any>", "<any>"))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BaseMarker):
            return NotImplemented

        return isinstance(other, AnyMarker)


class EmptyMarker(BaseMarker):
    def intersect(self, other: BaseMarker) -> BaseMarker:
        return self

    def union(self, other: BaseMarker) -> BaseMarker:
        return other

    def is_any(self) -> bool:
        return False

    def is_empty(self) -> bool:
        return True

    def validate(self, environment: dict[str, Any] | None) -> bool:
        return False

    def without_extras(self) -> BaseMarker:
        return self

    def exclude(self, marker_name: str) -> EmptyMarker:
        return self

    def only(self, *marker_names: str) -> EmptyMarker:
        return self

    def invert(self) -> AnyMarker:
        return AnyMarker()

    def __str__(self) -> str:
        return "<empty>"

    def __repr__(self) -> str:
        return "<EmptyMarker>"

    def __hash__(self) -> int:
        return hash(("<empty>", "<empty>"))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BaseMarker):
            return NotImplemented

        return isinstance(other, EmptyMarker)


class SingleMarker(BaseMarker):
    _CONSTRAINT_RE = re.compile(r"(?i)^(~=|!=|>=?|<=?|==?=?|in|not in)?\s*(.+)$")
    _VERSION_LIKE_MARKER_NAME = {
        "python_version",
        "python_full_version",
        "platform_release",
    }

    def __init__(
        self, name: str, constraint: str | BaseConstraint | VersionConstraint
    ) -> None:
        from poetry.core.constraints.generic import (
            parse_constraint as parse_generic_constraint,
        )
        from poetry.core.constraints.version import (
            parse_constraint as parse_version_constraint,
        )

        self._constraint: BaseConstraint | VersionConstraint
        self._parser: Callable[[str], BaseConstraint | VersionConstraint]
        self._name = ALIASES.get(name, name)
        constraint_string = str(constraint)

        # Extract operator and value
        m = self._CONSTRAINT_RE.match(constraint_string)
        if m is None:
            raise InvalidMarker(f"Invalid marker '{constraint_string}'")

        self._operator = m.group(1)
        if self._operator is None:
            self._operator = "=="

        self._value = m.group(2)
        self._parser = parse_generic_constraint

        if name in self._VERSION_LIKE_MARKER_NAME:
            self._parser = parse_version_constraint

            if self._operator in {"in", "not in"}:
                versions = []
                for v in re.split("[ ,]+", self._value):
                    split = v.split(".")
                    if len(split) in [1, 2]:
                        split.append("*")
                        op = "" if self._operator == "in" else "!="
                    else:
                        op = "==" if self._operator == "in" else "!="

                    versions.append(op + ".".join(split))

                glue = ", "
                if self._operator == "in":
                    glue = " || "

                self._constraint = self._parser(glue.join(versions))
            else:
                self._constraint = self._parser(constraint_string)
        else:
            # if we have a in/not in operator we split the constraint
            # into a union/multi-constraint of single constraint
            if self._operator in {"in", "not in"}:
                op, glue = ("==", " || ") if self._operator == "in" else ("!=", ", ")
                values = re.split("[ ,]+", self._value)
                constraint_string = glue.join(f"{op} {value}" for value in values)

            self._constraint = self._parser(constraint_string)

    @property
    def name(self) -> str:
        return self._name

    @property
    def constraint(self) -> BaseConstraint | VersionConstraint:
        return self._constraint

    @property
    def operator(self) -> str:
        return self._operator

    @property
    def value(self) -> str:
        return self._value

    def intersect(self, other: BaseMarker) -> BaseMarker:
        if isinstance(other, SingleMarker):
            merged = _merge_single_markers(self, other, MultiMarker)
            if merged is not None:
                return merged

            return MultiMarker(self, other)

        return other.intersect(self)

    def union(self, other: BaseMarker) -> BaseMarker:
        if isinstance(other, SingleMarker):
            merged = _merge_single_markers(self, other, MarkerUnion)
            if merged is not None:
                return merged

            return MarkerUnion(self, other)

        return other.union(self)

    def validate(self, environment: dict[str, Any] | None) -> bool:
        if environment is None:
            return True

        if self._name not in environment:
            return True

        # The type of constraint returned by the parser matches our constraint: either
        # both are BaseConstraint or both are VersionConstraint.  But it's hard for mypy
        # to know that.
        constraint = self._parser(environment[self._name])
        return self._constraint.allows(constraint)  # type: ignore[arg-type]

    def without_extras(self) -> BaseMarker:
        return self.exclude("extra")

    def exclude(self, marker_name: str) -> BaseMarker:
        if self.name == marker_name:
            return AnyMarker()

        return self

    def only(self, *marker_names: str) -> SingleMarker | AnyMarker:
        if self.name not in marker_names:
            return AnyMarker()

        return self

    def invert(self) -> BaseMarker:
        if self._operator in ("===", "=="):
            operator = "!="
        elif self._operator == "!=":
            operator = "=="
        elif self._operator == ">":
            operator = "<="
        elif self._operator == ">=":
            operator = "<"
        elif self._operator == "<":
            operator = ">="
        elif self._operator == "<=":
            operator = ">"
        elif self._operator == "in":
            operator = "not in"
        elif self._operator == "not in":
            operator = "in"
        elif self._operator == "~=":
            # This one is more tricky to handle
            # since it's technically a multi marker
            # so the inverse will be a union of inverse
            from poetry.core.constraints.version import VersionRangeConstraint

            if not isinstance(self._constraint, VersionRangeConstraint):
                # The constraint must be a version range, otherwise
                # it's an internal error
                raise RuntimeError(
                    "The '~=' operator should only represent version ranges"
                )

            min_ = self._constraint.min
            min_operator = ">=" if self._constraint.include_min else ">"
            max_ = self._constraint.max
            max_operator = "<=" if self._constraint.include_max else "<"

            return MultiMarker(
                SingleMarker(self._name, f"{min_operator} {min_}"),
                SingleMarker(self._name, f"{max_operator} {max_}"),
            ).invert()
        else:
            # We should never go there
            raise RuntimeError(f"Invalid marker operator '{self._operator}'")

        return parse_marker(f"{self._name} {operator} '{self._value}'")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SingleMarker):
            return False

        return self._name == other.name and self._constraint == other.constraint

    def __hash__(self) -> int:
        return hash((self._name, self._constraint))

    def __str__(self) -> str:
        return f'{self._name} {self._operator} "{self._value}"'


def _flatten_markers(
    markers: Iterable[BaseMarker],
    flatten_class: type[MarkerUnion | MultiMarker],
) -> list[BaseMarker]:
    flattened = []

    for marker in markers:
        if isinstance(marker, flatten_class):
            for _marker in _flatten_markers(
                marker.markers,  # type: ignore[attr-defined]
                flatten_class,
            ):
                if _marker not in flattened:
                    flattened.append(_marker)

        elif marker not in flattened:
            flattened.append(marker)

    return flattened


class MultiMarker(BaseMarker):
    def __init__(self, *markers: BaseMarker) -> None:
        self._markers = _flatten_markers(markers, MultiMarker)

    @property
    def markers(self) -> list[BaseMarker]:
        return self._markers

    @classmethod
    def of(cls, *markers: BaseMarker) -> BaseMarker:
        new_markers = _flatten_markers(markers, MultiMarker)
        old_markers: list[BaseMarker] = []

        while old_markers != new_markers:
            old_markers = new_markers
            new_markers = []
            for marker in old_markers:
                if marker in new_markers:
                    continue

                if marker.is_any():
                    continue

                intersected = False
                for i, mark in enumerate(new_markers):
                    # If we have a SingleMarker then with any luck after intersection
                    # it'll become another SingleMarker.
                    if isinstance(mark, SingleMarker):
                        new_marker = marker.intersect(mark)
                        if new_marker.is_empty():
                            return EmptyMarker()

                        if isinstance(new_marker, SingleMarker):
                            new_markers[i] = new_marker
                            intersected = True
                            break

                if intersected:
                    continue

                new_markers.append(marker)

        if any(m.is_empty() for m in new_markers):
            return EmptyMarker()

        if not new_markers:
            return AnyMarker()

        if len(new_markers) == 1:
            return new_markers[0]

        return MultiMarker(*new_markers)

    def intersect(self, other: BaseMarker) -> BaseMarker:
        return intersection(self, other)

    def union(self, other: BaseMarker) -> BaseMarker:
        return union(self, other)

    def union_simplify(self, other: BaseMarker) -> BaseMarker | None:
        """
        Finds a couple of easy simplifications for union on MultiMarkers:

            - union with any marker that appears as part of the multi is just that
              marker

            - union between two multimarkers where one is contained by the other is just
              the larger of the two
        """
        if other in self._markers:
            return other

        if isinstance(other, MultiMarker):
            our_markers = set(self.markers)
            their_markers = set(other.markers)

            if our_markers.issubset(their_markers):
                return self

            if their_markers.issubset(our_markers):
                return other

        return None

    def validate(self, environment: dict[str, Any] | None) -> bool:
        return all(m.validate(environment) for m in self._markers)

    def without_extras(self) -> BaseMarker:
        return self.exclude("extra")

    def exclude(self, marker_name: str) -> BaseMarker:
        new_markers = []

        for m in self._markers:
            if isinstance(m, SingleMarker) and m.name == marker_name:
                # The marker is not relevant since it must be excluded
                continue

            marker = m.exclude(marker_name)

            if not marker.is_empty():
                new_markers.append(marker)

        return self.of(*new_markers)

    def only(self, *marker_names: str) -> BaseMarker:
        new_markers = []

        for m in self._markers:
            if isinstance(m, SingleMarker) and m.name not in marker_names:
                # The marker is not relevant since it's not one we want
                continue

            marker = m.only(*marker_names)

            if not marker.is_empty():
                new_markers.append(marker)

        if not new_markers:
            return EmptyMarker()

        return self.of(*new_markers)

    def invert(self) -> BaseMarker:
        markers = [marker.invert() for marker in self._markers]

        return MarkerUnion(*markers)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MultiMarker):
            return False

        return set(self._markers) == set(other.markers)

    def __hash__(self) -> int:
        h = hash("multi")
        for m in self._markers:
            h ^= hash(m)

        return h

    def __str__(self) -> str:
        elements = []
        for m in self._markers:
            if isinstance(m, (SingleMarker, MultiMarker)):
                elements.append(str(m))
            else:
                elements.append(f"({str(m)})")

        return " and ".join(elements)


class MarkerUnion(BaseMarker):
    def __init__(self, *markers: BaseMarker) -> None:
        self._markers = _flatten_markers(markers, MarkerUnion)

    @property
    def markers(self) -> list[BaseMarker]:
        return self._markers

    @classmethod
    def of(cls, *markers: BaseMarker) -> BaseMarker:
        new_markers = _flatten_markers(markers, MarkerUnion)
        old_markers: list[BaseMarker] = []

        while old_markers != new_markers:
            old_markers = new_markers
            new_markers = []
            for marker in old_markers:
                if marker in new_markers:
                    continue

                if marker.is_empty():
                    continue

                included = False
                for i, mark in enumerate(new_markers):
                    # If we have a SingleMarker then with any luck after union it'll
                    # become another SingleMarker.
                    if isinstance(mark, SingleMarker):
                        new_marker = marker.union(mark)
                        if new_marker.is_any():
                            return AnyMarker()

                        if isinstance(new_marker, SingleMarker):
                            new_markers[i] = new_marker
                            included = True
                            break

                    # If we have a MultiMarker then we can look for the simplifications
                    # implemented in union_simplify().
                    elif isinstance(mark, MultiMarker):
                        union = mark.union_simplify(marker)
                        if union is not None:
                            new_markers[i] = union
                            included = True
                            break

                if included:
                    # flatten again because union_simplify may return a union
                    new_markers = _flatten_markers(new_markers, MarkerUnion)
                    continue

                new_markers.append(marker)

        if any(m.is_any() for m in new_markers):
            return AnyMarker()

        if not new_markers:
            return EmptyMarker()

        if len(new_markers) == 1:
            return new_markers[0]

        return MarkerUnion(*new_markers)

    def append(self, marker: BaseMarker) -> None:
        if marker in self._markers:
            return

        self._markers.append(marker)

    def intersect(self, other: BaseMarker) -> BaseMarker:
        return intersection(self, other)

    def union(self, other: BaseMarker) -> BaseMarker:
        return union(self, other)

    def validate(self, environment: dict[str, Any] | None) -> bool:
        return any(m.validate(environment) for m in self._markers)

    def without_extras(self) -> BaseMarker:
        return self.exclude("extra")

    def exclude(self, marker_name: str) -> BaseMarker:
        new_markers = []

        for m in self._markers:
            if isinstance(m, SingleMarker) and m.name == marker_name:
                # The marker is not relevant since it must be excluded
                continue

            marker = m.exclude(marker_name)
            new_markers.append(marker)

        if not new_markers:
            # All markers were the excluded marker.
            return AnyMarker()

        return self.of(*new_markers)

    def only(self, *marker_names: str) -> BaseMarker:
        new_markers = []

        for m in self._markers:
            if isinstance(m, SingleMarker) and m.name not in marker_names:
                # The marker is not relevant since it's not one we want
                continue

            marker = m.only(*marker_names)

            if not marker.is_empty():
                new_markers.append(marker)

        return self.of(*new_markers)

    def invert(self) -> BaseMarker:
        markers = [marker.invert() for marker in self._markers]
        return MultiMarker(*markers)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MarkerUnion):
            return False

        return set(self._markers) == set(other.markers)

    def __hash__(self) -> int:
        h = hash("union")
        for m in self._markers:
            h ^= hash(m)

        return h

    def __str__(self) -> str:
        return " or ".join(
            str(m) for m in self._markers if not m.is_any() and not m.is_empty()
        )

    def is_any(self) -> bool:
        return any(m.is_any() for m in self._markers)

    def is_empty(self) -> bool:
        return all(m.is_empty() for m in self._markers)


def parse_marker(marker: str) -> BaseMarker:
    if marker == "<empty>":
        return EmptyMarker()

    if not marker or marker == "*":
        return AnyMarker()

    parsed = _parser.parse(marker)

    markers = _compact_markers(parsed.children)

    return markers


def _compact_markers(
    tree_elements: Tree, tree_prefix: str = "", top_level: bool = True
) -> BaseMarker:
    from lark import Token

    # groups is a disjunction of conjunctions
    # eg [[A, B], [C, D]] represents "(A and B) or (C and D)"
    groups: list[list[BaseMarker]] = [[]]

    for token in tree_elements:
        if isinstance(token, Token):
            if token.type == f"{tree_prefix}BOOL_OP" and token.value == "or":
                groups.append([])

            continue

        if token.data == "marker":
            sub_marker = _compact_markers(
                token.children, tree_prefix=tree_prefix, top_level=False
            )
            groups[-1].append(sub_marker)

        elif token.data == f"{tree_prefix}item":
            name, op, value = token.children
            if value.type == f"{tree_prefix}MARKER_NAME":
                name, value, = (
                    value,
                    name,
                )

            value = value[1:-1]
            sub_marker = SingleMarker(str(name), f"{op}{value}")
            groups[-1].append(sub_marker)

        elif token.data == f"{tree_prefix}BOOL_OP" and token.children[0] == "or":
            groups.append([])

    # Combine the groups.
    sub_markers = [MultiMarker(*group) for group in groups]

    # This function calls itself recursively. In the inner calls we don't perform any
    # simplification, instead doing it all only when we have the complete marker.
    if not top_level:
        return MarkerUnion(*sub_markers)

    return union(*sub_markers)


def cnf(marker: BaseMarker) -> BaseMarker:
    """Transforms the marker into CNF (conjunctive normal form)."""
    if isinstance(marker, MarkerUnion):
        cnf_markers = [cnf(m) for m in marker.markers]
        sub_marker_lists = [
            m.markers if isinstance(m, MultiMarker) else [m] for m in cnf_markers
        ]
        return MultiMarker.of(
            *[MarkerUnion.of(*c) for c in itertools.product(*sub_marker_lists)]
        )

    if isinstance(marker, MultiMarker):
        return MultiMarker.of(*[cnf(m) for m in marker.markers])

    return marker


def dnf(marker: BaseMarker) -> BaseMarker:
    """Transforms the marker into DNF (disjunctive normal form)."""
    if isinstance(marker, MultiMarker):
        dnf_markers = [dnf(m) for m in marker.markers]
        sub_marker_lists = [
            m.markers if isinstance(m, MarkerUnion) else [m] for m in dnf_markers
        ]
        return MarkerUnion.of(
            *[MultiMarker.of(*c) for c in itertools.product(*sub_marker_lists)]
        )
    if isinstance(marker, MarkerUnion):
        return MarkerUnion.of(*[dnf(m) for m in marker.markers])
    return marker


def intersection(*markers: BaseMarker) -> BaseMarker:
    return dnf(MultiMarker(*markers))


def union(*markers: BaseMarker) -> BaseMarker:
    conjunction = cnf(MarkerUnion(*markers))
    if not isinstance(conjunction, MultiMarker):
        return conjunction

    return dnf(conjunction)


def _merge_single_markers(
    marker1: SingleMarker,
    marker2: SingleMarker,
    merge_class: type[MultiMarker | MarkerUnion],
) -> BaseMarker | None:
    if {marker1.name, marker2.name} == PYTHON_VERSION_MARKERS:
        return _merge_python_version_single_markers(marker1, marker2, merge_class)

    if marker1.name != marker2.name:
        return None

    if merge_class == MultiMarker:
        merge_method = marker1.constraint.intersect
    else:
        merge_method = marker1.constraint.union
    # Markers with the same name have the same constraint type,
    # but mypy can't see that.
    result_constraint = merge_method(marker2.constraint)  # type: ignore[arg-type]

    result_marker: BaseMarker | None = None
    if result_constraint.is_empty():
        result_marker = EmptyMarker()
    elif result_constraint.is_any():
        result_marker = AnyMarker()
    elif result_constraint == marker1.constraint:
        result_marker = marker1
    elif result_constraint == marker2.constraint:
        result_marker = marker2
    elif (
        isinstance(result_constraint, VersionConstraint)
        and result_constraint.is_simple()
    ):
        result_marker = SingleMarker(marker1.name, result_constraint)
    return result_marker


def _merge_python_version_single_markers(
    marker1: SingleMarker,
    marker2: SingleMarker,
    merge_class: type[MultiMarker | MarkerUnion],
) -> BaseMarker | None:
    from poetry.core.packages.utils.utils import get_python_constraint_from_marker

    if marker1.name == "python_version":
        version_marker = marker1
        full_version_marker = marker2
    else:
        version_marker = marker2
        full_version_marker = marker1

    normalized_constraint = get_python_constraint_from_marker(version_marker)
    normalized_marker = SingleMarker("python_full_version", normalized_constraint)
    merged_marker = _merge_single_markers(
        normalized_marker, full_version_marker, merge_class
    )
    if merged_marker == normalized_marker:
        # prefer original marker to avoid unnecessary changes
        return version_marker
    if merged_marker and isinstance(merged_marker, SingleMarker):
        # We have to fix markers like 'python_full_version == "3.6"'
        # to receive 'python_full_version == "3.6.0"'.
        # It seems a bit hacky to convert to string and back to marker,
        # but it's probably much simpler than to consider the different constraint
        # classes (mostly VersonRangeConstraint, but VersionUnion for "!=") and
        # since this conversion is only required for python_full_version markers
        # it may be sufficient to handle it here.
        marker_string = str(merged_marker)
        precision = marker_string.count(".") + 1
        if precision < 3:
            marker_string = marker_string[:-1] + ".0" * (3 - precision) + '"'
            merged_marker = parse_marker(marker_string)
    return merged_marker

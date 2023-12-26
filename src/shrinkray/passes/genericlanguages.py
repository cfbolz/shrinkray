"""
Module of reduction passes designed for "things that look like programming languages".
"""

import re
from functools import wraps
from string import ascii_lowercase, ascii_uppercase
from typing import AnyStr, Callable

import trio
from attr import define

from shrinkray.passes.bytes import ByteReplacement
from shrinkray.passes.definitions import ReductionPass
from shrinkray.passes.patching import apply_patches
from shrinkray.problem import (
    BasicReductionProblem,
    Format,
    ParseError,
    ReductionProblem,
)
from shrinkray.work import NotFound


@define(frozen=True)
class Substring(Format[AnyStr, AnyStr]):
    prefix: AnyStr
    suffix: AnyStr

    @property
    def name(self) -> str:
        return f"Substring({len(self.prefix)}, {len(self.suffix)})"

    def parse(self, input: AnyStr) -> AnyStr:
        if input.startswith(self.prefix) and input.endswith(self.suffix):
            return input[len(self.prefix) : len(input) - len(self.suffix)]
        else:
            raise ParseError()

    def dumps(self, input: AnyStr) -> AnyStr:
        return self.prefix + input + self.suffix


def regex_pass(
    pattern: AnyStr | re.Pattern[AnyStr],
    flags: re.RegexFlag = 0,
) -> Callable[[ReductionPass[AnyStr]], ReductionPass[AnyStr]]:
    if not isinstance(pattern, re.Pattern):
        pattern = re.compile(pattern, flags=flags)

    def inner(fn: ReductionPass[AnyStr]) -> ReductionPass[AnyStr]:
        @wraps(fn)
        async def reduction_pass(problem: ReductionProblem[AnyStr]) -> None:
            matching_regions = []

            i = 0
            while i < len(problem.current_test_case):
                search = pattern.search(problem.current_test_case, i)
                if search is None:
                    break

                u, v = search.span()
                matching_regions.append((u, v))

                i = v

            if not matching_regions:
                return

            initial = problem.current_test_case

            replacements = [initial[u:v] for u, v in matching_regions]

            def replace(i: int, s: AnyStr) -> AnyStr:
                empty = initial[:0]

                parts = []

                prev = 0
                for j, (u, v) in enumerate(matching_regions):
                    parts.append(initial[prev:u])
                    if j != i:
                        parts.append(replacements[j])
                    else:
                        parts.append(s)
                    prev = v

                parts.append(initial[prev:])

                return empty.join(parts)

            async with trio.open_nursery() as nursery:
                current_merge_attempts = 0

                async def reduce_region(i: int) -> None:
                    async def is_interesting(s: AnyStr) -> bool:
                        nonlocal current_merge_attempts
                        is_merging = False
                        retries = 0
                        try:
                            while True:
                                # Other tasks may have updated the test case, so when we
                                # check whether something is interesting but it doesn't update
                                # the test case, this means something has changed. Given that
                                # we found a promising reduction, it's likely to be worth trying
                                # again. In theory an uninteresting test case could also become
                                # interesting if the underlying test case changes, but that's
                                # not likely enough to be worth checking.
                                while not is_merging and current_merge_attempts > 0:
                                    await trio.sleep(0.01)

                                attempt = replace(i, s)
                                if not await problem.is_interesting(attempt):
                                    return False
                                if replace(i, s) == attempt:
                                    replacements[i] = s
                                    return True
                                if not is_merging:
                                    is_merging = True
                                    current_merge_attempts += 1

                                retries += 1

                                # If we've retried this many times then something has gone seriously
                                # wrong with our concurrency approach and it's probably a bug.
                                assert retries <= 100
                        finally:
                            if is_merging:
                                current_merge_attempts -= 1
                                assert current_merge_attempts >= 0

                    subproblem = BasicReductionProblem(
                        replacements[i],
                        is_interesting,
                        work=problem.work,
                    )
                    nursery.start_soon(fn, subproblem)

                for i in range(len(matching_regions)):
                    await reduce_region(i)

        return reduction_pass

    return inner


async def reduce_integer(problem: ReductionProblem[int]) -> None:
    assert problem.current_test_case >= 0

    if await problem.is_interesting(0):
        return

    lo = 0
    hi = problem.current_test_case

    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if await problem.is_interesting(mid):
            hi = mid
        else:
            lo = mid

        if await problem.is_interesting(hi - 1):
            hi -= 1

        if await problem.is_interesting(lo + 1):
            return
        else:
            lo += 1


class IntegerFormat(Format[bytes, int]):
    def parse(self, input: bytes) -> int:
        try:
            return int(input.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            raise ParseError()

    def dumps(self, input: int) -> bytes:
        return str(input).encode("ascii")


@regex_pass(b"[0-9]+")
async def reduce_integer_literals(problem: ReductionProblem[bytes]) -> None:
    await reduce_integer(problem.view(IntegerFormat()))


@regex_pass(rb"[0-9]+ [*+-/] [0-9]+")
async def combine_expressions(problem: ReductionProblem[bytes]) -> None:
    try:
        # NB: Use of eval is safe, as everything passed to this is a simple
        # arithmetic expression. Would ideally replace with a guaranteed
        # safe version though.
        await problem.is_interesting(
            str(eval(problem.current_test_case)).encode("ascii")
        )
    except ArithmeticError:
        pass


@regex_pass(rb'([\'"])\s*\1')
async def merge_adjacent_strings(problem: ReductionProblem[bytes]) -> None:
    await problem.is_interesting(b"")


@regex_pass(rb"''|\"\"|false|\(\)|\[\]", re.IGNORECASE)
async def replace_falsey_with_zero(problem: ReductionProblem[bytes]) -> None:
    await problem.is_interesting(b"0")


async def simplify_brackets(problem: ReductionProblem[bytes]) -> None:
    bracket_types = [b"[]", b"{}", b"()"]

    patches = [dict(zip(u, v)) for u in bracket_types for v in bracket_types if u > v]

    await apply_patches(problem, ByteReplacement(), patches)


IDENTIFIER = re.compile(rb"(\b[A-Za-z][A-Za-z0-9_]*\b)|([0-9]+)")


def shortlex(s):
    return (len(s), s)


async def normalize_identifiers(problem: ReductionProblem[bytes]) -> None:
    identifiers = {m.group(0) for m in IDENTIFIER.finditer(problem.current_test_case)}
    replacements = set(identifiers)

    for char_type in [ascii_lowercase, ascii_uppercase]:
        for cc in char_type.encode("ascii"):
            c = bytes([cc])
            if c not in replacements:
                replacements.add(c)
                break

    replacements = sorted(replacements, key=shortlex)
    targets = sorted(identifiers, key=shortlex, reverse=True)

    # TODO: This could use better parallelisation.
    for t in targets:
        pattern = re.compile(rb"\b" + t + rb"\b")
        source = problem.current_test_case
        if not pattern.search(source):
            continue

        async def can_replace(r):
            if shortlex(r) >= shortlex(t):
                return False
            attempt = pattern.sub(r, source)
            assert attempt != source
            return await problem.is_interesting(attempt)

        try:
            await problem.work.find_first_value(replacements, can_replace)
        except NotFound:
            pass

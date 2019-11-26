"""Odds and ends common to all of ndscan."""

from artiq.language import units
from enum import Enum, unique
import oitg.fitting
from typing import Any, Callable, Dict, Iterable

#: Registry of well-known fit procecure names.
FIT_OBJECTS = {
    n: getattr(oitg.fitting, n)
    for n in [
        "cos", "decaying_sinusoid", "exponential_decay", "line", "lorentzian",
        "rabi_flop", "sinusoid"
    ]
}
FIT_OBJECTS["parabola"] = oitg.fitting.shifted_parabola

#: Name of the ``artiq.language.HasEnvironment`` argument that is used to confer the
#: list of available parameters to the dashboard plugin, and to pass the information
#: about scanned and overridden parameters to the :class:`FragmentScanExperiment`
#: when it is launched.
#:
#: Users should not need to directly interface with this.
PARAMS_ARG_KEY = "ndscan_params"


@unique
class NoAxesMode(Enum):
    """Behaviours when launching an experiment with no parameter to be scanned."""
    single = "Single (run once)"
    repeat = "Repeat (save only last)"
    time_series = "Time series (save all, with timestamps)"


def strip_prefix(string: str, prefix: str) -> str:
    if string.startswith(prefix):
        return string[len(prefix):]
    return string


def strip_suffix(string: str, suffix: str) -> str:
    if string.endswith(suffix):
        return string[:-len(suffix)]
    return string


def shorten_to_unambiguous_suffixes(fqns: Iterable[str],
                                    get_last_n_parts: Callable[[str, int], str]
                                    ) -> Dict[str, str]:
    short_to_fqns = dict()
    shortened_fqns = dict()

    for current in fqns:
        if current in shortened_fqns:
            continue

        n = 1
        while True:
            candidate = get_last_n_parts(current, n)
            if candidate not in short_to_fqns:
                short_to_fqns[candidate] = {current}
                shortened_fqns[current] = candidate
                break

            # We have a conflict, disambiguate.
            existing_fqns = short_to_fqns[candidate]
            for old in existing_fqns:
                if shortened_fqns[old] == candidate:
                    # This hasn't previously been moved to a higher n, so
                    # do it now.
                    new = get_last_n_parts(old, n + 1)
                    shortened_fqns[old] = new
                    short_to_fqns[new] = {old}
                    break  # Exits inner for loop.
            existing_fqns.add(current)
            n += 1

    return shortened_fqns


def eval_param_default(value: str, get_dataset: Callable) -> Any:
    env = {name: getattr(units, name) for name in units.__all__}
    env.update({"dataset": get_dataset})
    return eval(value, env)

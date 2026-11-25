#!/usr/bin/env python3
"""Probe how standard library implementations treat integer types.

Produces the implementation tables of D4497R0. Every cell is one small
translation unit compiled with -fsyntax-only (or /Zs for MSVC), so the result
says only whether the standard library accepts the code, not whether it
behaves correctly at run time.

    probes/D4497R0/run_matrix.py          # libc++ and libstdc++ (local)
    probes/D4497R0/run_matrix.py --msvc   # also MSVC STL via Compiler Explorer

Results go to probes/D4497R0/results.json and results.md.

Cell values:
    yes / no    a trait or concept evaluated to true / false
    ok          the code compiled
    rejected    the first error is in the probe itself (no viable function,
                unsatisfied constraint, ambiguous call, ...)
    lib-error   the first error is inside a library header (a static_assert
                or an instantiation failure), i.e. a hard error

Toolchain paths can be overridden with CLANG, GXX, GCC_INCLUDE and
GCC_TARGET_INCLUDE.
"""

import argparse
import concurrent.futures
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent

CLANG = os.environ.get("CLANG", "/opt/homebrew/opt/llvm/bin/clang++")
GXX = os.environ.get("GXX", "/opt/homebrew/bin/g++-16")
GCC_INCLUDE = os.environ.get("GCC_INCLUDE", "/opt/homebrew/opt/gcc/include/c++/16")
GCC_TARGET_INCLUDE = os.environ.get(
    "GCC_TARGET_INCLUDE", "/opt/homebrew/opt/gcc/include/c++/16/aarch64-apple-darwin27")
CLANG_TARGET = os.environ.get("CLANG_TARGET", "x86_64-apple-darwin")
MSVC_ID = os.environ.get("MSVC_ID", "vcpp_v19_latest_x64")
MSVC_FLAGS = "/std:c++latest /EHsc /Zs"
CE_URL = "https://godbolt.org/api/compiler/{}/compile"

MODES = ["c++26", "gnu++26"]

# name: (spelling, signedness, width, extra header)
TYPES = {
    "i128": ("__int128", "signed", 128, None),
    "u128": ("unsigned __int128", "unsigned", 128, None),
    "b32": ("_BitInt(32)", "signed", 32, None),
    "ub32": ("unsigned _BitInt(32)", "unsigned", 32, None),
    "b128": ("_BitInt(128)", "signed", 128, None),
    "ub128": ("unsigned _BitInt(128)", "unsigned", 128, None),
    "b256": ("_BitInt(256)", "signed", 256, None),
    "ub256": ("unsigned _BitInt(256)", "unsigned", 256, None),
    "s128c": ("std::_Signed128", "signed", 128, "__msvc_int128.hpp"),
    "u128c": ("std::_Unsigned128", "unsigned", 128, "__msvc_int128.hpp"),
}

# Column groups as printed: (heading, signed type, unsigned type).
PAIRS = [
    ("`__int128`", "i128", "u128"),
    ("`_BitInt(32)`", "b32", "ub32"),
    ("`_BitInt(128)`", "b128", "ub128"),
    ("`_BitInt(256)`", "b256", "ub256"),
]
MSVC_PAIRS = [("`std::_Signed128` / `_Unsigned128`", "s128c", "u128c")]

# Each probe: id, label, headers, kind ("value" or "valid"), code, applies.
# In code, T is the type under test and W its width.
PROBES = [
    ("is_integral", "`is_integral_v<T>`", ["type_traits"], "value",
     "std::is_integral_v<T>", "both"),
    ("is_signed", "`is_signed_v<T>`", ["type_traits"], "value",
     "std::is_signed_v<T>", "both"),
    ("make_unsigned", "`make_unsigned_t<T>`", ["type_traits"], "valid",
     "using X = std::make_unsigned_t<T>; static_assert(sizeof(X) == sizeof(T));", "both"),
    ("integral", "`std::integral<T>`", ["concepts"], "value",
     "std::integral<T>", "both"),
    ("limits", "`numeric_limits<T>::is_specialized`", ["limits"], "value",
     "std::numeric_limits<T>::is_specialized", "both"),
    ("limits_digits", "`numeric_limits<T>::digits` correct", ["limits", "type_traits"], "value",
     "std::numeric_limits<T>::digits == (std::numeric_limits<T>::is_signed ? W - 1 : W)", "both"),
    ("cmp_less", "`cmp_less(T, int)`", ["utility"], "valid",
     "(void)std::cmp_less(T{}, 0);", "both"),
    ("in_range", "`in_range<T>(int)`", ["utility"], "valid",
     "(void)std::in_range<T>(0);", "both"),
    ("bit", "`popcount`, `countl_zero`, `bit_width`, `bit_ceil`, `rotl`", ["bit"], "valid",
     "T x{}; (void)std::popcount(x); (void)std::countl_zero(x); (void)std::bit_width(x);"
     " (void)std::bit_ceil(T{1}); (void)std::rotl(x, 1);", "unsigned"),
    ("byteswap", "`byteswap`", ["bit"], "valid",
     "(void)std::byteswap(T{});", "both"),
    ("stdbit", "`stdc_count_ones` (`<stdbit.h>`)", ["stdbit.h"], "valid",
     "(void)stdc_count_ones(T{});", "unsigned"),
    ("gcd", "`gcd`, `lcm`", ["numeric"], "valid",
     "(void)std::gcd(T{1}, T{1}); (void)std::lcm(T{1}, T{1});", "both"),
    ("midpoint", "`midpoint`", ["numeric"], "valid",
     "(void)std::midpoint(T{}, T{});", "both"),
    ("saturating_add", "`saturating_add`", ["numeric"], "valid",
     "(void)std::saturating_add(T{}, T{});", "both"),
    ("add_sat", "`add_sat` (pre-[@P4052R0] name)", ["numeric"], "valid",
     "(void)std::add_sat(T{}, T{});", "both"),
    ("saturating_cast", "`saturating_cast<T>`", ["numeric"], "valid",
     "(void)std::saturating_cast<T>(0);", "both"),
    ("ckd_add", "`ckd_add` (`<stdckdint.h>`)", ["stdckdint.h"], "valid",
     "T r; (void)ckd_add(&r, T{}, T{});", "both"),
    ("abs", "`abs`", ["cstdlib"], "valid",
     "(void)std::abs(T{});", "signed"),
    ("cmath", "`sqrt` (integral overload)", ["cmath"], "valid",
     "(void)std::sqrt(T{});", "both"),
    ("to_chars", "`to_chars`", ["charconv"], "valid",
     "char b[512]; (void)std::to_chars(b, b + 512, T{});", "both"),
    ("from_chars", "`from_chars`", ["charconv"], "valid",
     "const char b[] = \"1\"; T v; (void)std::from_chars(b, b + 1, v);", "both"),
    ("to_string", "`to_string`", ["string"], "valid",
     "(void)std::to_string(T{});", "both"),
    ("format", "`format(\"{}\", T)`", ["format"], "valid",
     "(void)std::format(\"{}\", T{});", "both"),
    ("ostream", "`ostream << T`", ["sstream"], "valid",
     "std::ostringstream os; os << T{};", "both"),
    ("istream", "`istream >> T`", ["sstream"], "valid",
     "std::istringstream is; T v; is >> v;", "both"),
    ("hash", "`hash<T>`", ["functional"], "valid",
     "(void)std::hash<T>{}(T{});", "both"),
    ("random", "`uniform_int_distribution<T>`", ["random"], "valid",
     "std::mt19937 g; std::uniform_int_distribution<T> d; (void)d(g);", "both"),
    ("atomic", "`atomic<T>::fetch_add`", ["atomic"], "valid",
     "std::atomic<T> a{}; (void)a.fetch_add(T{1});", "both"),
    ("iota", "`views::iota(T, T)`", ["ranges"], "valid",
     "auto v = std::views::iota(T{}, T{1}); (void)v.size();", "both"),
    ("to_integer", "`to_integer<T>(byte)`", ["cstddef"], "valid",
     "(void)std::to_integer<T>(std::byte{});", "both"),
    ("extents", "`extents<T, 2>`", ["mdspan"], "valid",
     "std::extents<T, 2> e; (void)e;", "both"),
    ("duration", "`chrono::duration<T>`", ["chrono"], "valid",
     "std::chrono::duration<T> d{}; (void)(d + d);", "both"),
]

# Feature availability with standard types only: one TU prints the macros.
MACROS = [
    "__cpp_lib_integer_comparison_functions",
    "__cpp_lib_int_pow2",
    "__cpp_lib_byteswap",
    "__cpp_lib_bitops",
    "__cpp_lib_saturation_arithmetic",
    "__cpp_lib_stdckdint_h",
    "__cpp_lib_stdbit_h",
    "__cpp_lib_to_chars",
    "__cpp_lib_constexpr_charconv",
    "__cpp_lib_to_string",
    "__cpp_lib_format_uchar",
    "__cpp_lib_simd",
    "__cpp_lib_simd_bitops",
]

# Feature probes with standard types: id, label, headers, kind, code.
FEATURES = [
    ("saturating_add_int", "`std::saturating_add(1, 1)`", ["numeric"], "valid",
     "(void)std::saturating_add(1, 1);"),
    ("add_sat_int", "`std::add_sat(1, 1)` (old name)", ["numeric"], "valid",
     "(void)std::add_sat(1, 1);"),
    ("ckd_add_int", "`ckd_add` on `int`", ["stdckdint.h"], "valid",
     "int r; (void)ckd_add(&r, 1, 1);"),
    ("stdbit_uint", "`stdc_count_ones(1u)`", ["stdbit.h"], "valid",
     "(void)stdc_count_ones(1u);"),
    ("shl", "`std::shl(1u, 1)` ([@P3793R2])", ["bit"], "valid",
     "(void)std::shl(1u, 1);"),
    ("bit_reverse", "`std::bit_reverse(1u)` ([@P3104R6])", ["bit"], "valid",
     "(void)std::bit_reverse(1u);"),
    ("random_schar", "`uniform_int_distribution<signed char>` ([@P4037R1])", ["random"], "valid",
     "std::mt19937 g; std::uniform_int_distribution<signed char> d; (void)d(g);"),
    ("random_char", "`uniform_int_distribution<char>`", ["random"], "valid",
     "std::mt19937 g; std::uniform_int_distribution<char> d; (void)d(g);"),
    ("iota_diff_integral", "`iota_view<long long>` difference type is integral", ["ranges"], "value",
     "std::is_integral_v<std::ranges::range_difference_t<std::ranges::iota_view<long long, long long>>>"),
    ("iota_diff_width", "`iota_view<long long>` difference type is wider than 64 bits", ["ranges", "climits"], "value",
     "sizeof(std::ranges::range_difference_t<std::ranges::iota_view<long long, long long>>) * CHAR_BIT > 64"),
]

ERROR_RE = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?:\d+:)? (?:fatal )?error: (?P<msg>.*)$")
MSVC_ERROR_RE = re.compile(r"^(?P<file>.+?)\((?P<line>\d+)\)(?:\s*)?: (?:fatal )?error (?P<msg>.*)$")
VALUE_TAG = "D4497_VALUE_FALSE"


def source(headers, kind, code, spelling=None, width=None, extra_header=None):
    lines = []
    for h in headers:
        lines.append(f"#include <{h}>")
    if "type_traits" not in headers:
        lines.append("#include <type_traits>")
    if extra_header:
        lines.append(f"#include <{extra_header}>")
    if spelling is not None:
        lines.append(f"using T = {spelling};")
        lines.append(f"constexpr int W = {width};")
    if kind == "value":
        lines.append(f"constexpr bool probe_value = ({code});")
        lines.append(f'static_assert(probe_value, "{VALUE_TAG}");')
    else:
        lines.append("void probe() {")
        lines.append(f"    {code}")
        lines.append("}")
    lines.append("int main() {}")
    return "\n".join(lines) + "\n"


def classify(kind, ok, output, tu_name):
    """Turn compiler output into a cell value and the first error message."""
    if ok:
        return ("yes" if kind == "value" else "ok"), ""
    first = None
    for line in output.splitlines():
        m = ERROR_RE.match(line.strip()) or MSVC_ERROR_RE.match(line.strip())
        if m:
            first = m
            break
    tail = output.strip().splitlines()[-1:]
    msg = first.group("msg").strip() if first else (tail[0] if tail else "")
    # A value probe is false only if its own static_assert is the first error.
    if kind == "value" and first is not None and VALUE_TAG in msg:
        return "no", ""
    if "file not found" in msg or "Cannot open include file" in msg or "No such file" in msg:
        return "no-header", msg[:200]
    if first is None:
        return "lib-error", msg
    # Compiler Explorer names the probe "<source>" or "example.cpp".
    in_probe = pathlib.PurePath(first.group("file").replace("\\", "/")).name in (
        tu_name, "example.cpp", "<source>")
    return ("rejected" if in_probe else "lib-error"), msg[:200]


def local_toolchains():
    # Clang's BITINT_MAXWIDTH is 128 on AArch64 but 8388608 on x86-64, so the
    # Clang probes target x86-64 to reach _BitInt(256). Syntax checks only, so
    # no x86-64 sysroot is needed.
    libstdcxx_clang = [CLANG, f"--target={CLANG_TARGET}", "-stdlib=libstdc++", "-nostdinc++",
                       "-isystem", GCC_INCLUDE, "-isystem", GCC_TARGET_INCLUDE]
    bitint = ["b32", "ub32", "b128", "ub128", "b256", "ub256"]
    return {
        "libc++": {"cmd": [CLANG, f"--target={CLANG_TARGET}", "-stdlib=libc++"],
                   "types": ["i128", "u128"] + bitint},
        "libstdc++/gcc": {"cmd": [GXX], "types": ["i128", "u128"]},
        "libstdc++/clang": {"cmd": libstdcxx_clang, "types": ["i128", "u128"] + bitint},
    }


def version(cmd):
    out = subprocess.run([cmd[0], "--version"], capture_output=True, text=True).stdout
    return out.splitlines()[0] if out else "?"


def compile_local(cmd, mode, src, workdir, name):
    path = pathlib.Path(workdir) / name
    path.write_text(src)
    # -w hides warnings, but would also hide the #pragma message output that
    # the macro probe relies on.
    quiet = [] if name == "macros.cpp" else ["-w"]
    proc = subprocess.run(cmd + [f"-std={mode}", "-fsyntax-only"] + quiet + [str(path)],
                          capture_output=True, text=True)
    return proc.returncode == 0, proc.stderr


def compile_msvc(src):
    body = {"source": src,
            "options": {"userArguments": MSVC_FLAGS,
                        "compilerOptions": {"skipAsm": True},
                        "filters": {}}}
    req = urllib.request.Request(CE_URL.format(MSVC_ID), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                res = json.load(r)
            text = "\n".join(l.get("text", "") for l in res.get("stdout", []) + res.get("stderr", []))
            return res.get("code") == 0, text
        except Exception as e:  # network hiccup or rate limit
            time.sleep(5 * (attempt + 1))
            err = str(e)
    return False, f"compiler explorer request failed: {err}"


def type_cells(tool, cmd, types, mode, workdir):
    jobs = []
    for pid, _label, headers, kind, code, applies in PROBES:
        for t in types:
            spelling, sign, width, extra = TYPES[t]
            if applies != "both" and applies != sign:
                continue
            src = source(headers, kind, code, spelling, width, extra)
            name = f"{tool.replace('/', '_').replace('+', 'x')}_{mode}_{pid}_{t}.cpp"
            jobs.append((pid, t, kind, src, name))
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        futs = {ex.submit(compile_local, cmd, mode, src, workdir, name): (pid, t, kind, name)
                for pid, t, kind, src, name in jobs}
        for f in concurrent.futures.as_completed(futs):
            pid, t, kind, name = futs[f]
            ok, out = f.result()
            results[f"{pid}|{t}"] = classify(kind, ok, out, name)
    return results


def macro_source():
    lines = ["#include <version>",
             "#define D4497_STR2(x) #x",
             "#define D4497_STR(x) D4497_STR2(x)"]
    for m in MACROS:
        lines.append(f'#pragma message("D4497 {m}=" D4497_STR({m}))')
    lines.append("int main() {}")
    return "\n".join(lines) + "\n"


def parse_macros(output):
    found = {}
    for m in re.finditer(r"D4497 (__cpp_lib_\w+)=([\w]+)", output):
        name, value = m.group(1), m.group(2)
        found[name] = value if value != name else "-"
    return {m: found.get(m, "-") for m in MACROS}


def feature_cells(compile_fn):
    res = {}
    for fid, _label, headers, kind, code in FEATURES:
        src = source(headers, kind, code)
        ok, out = compile_fn(src, f"feature_{fid}.cpp")
        res[fid] = classify(kind, ok, out, f"feature_{fid}.cpp")
    ok, out = compile_fn(macro_source(), "macros.cpp")
    res["macros"] = parse_macros(out)
    return res


def run(args):
    results = {"toolchains": {}, "types": {}, "features": {}, "msvc_types": {}}
    with tempfile.TemporaryDirectory() as workdir:
        for tool, spec in local_toolchains().items():
            results["toolchains"][tool] = {"version": version(spec["cmd"]),
                                          "flags": " ".join(spec["cmd"][1:])}
            for mode in MODES:
                print(f"[{tool} -std={mode}] type probes", file=sys.stderr)
                results["types"][f"{tool}|{mode}"] = type_cells(tool, spec["cmd"], spec["types"], mode, workdir)
            mode = "c++26"
            print(f"[{tool} -std={mode}] feature probes", file=sys.stderr)
            results["features"][tool] = feature_cells(
                lambda src, name, c=spec["cmd"]: compile_local(c, mode, src, workdir, name))
    if args.msvc:
        # Record the exact compiler and STL versions behind the moving
        # Compiler Explorer "latest" alias.
        ok, out = compile_msvc("#include <version>\n#define D4497_S2(x) #x\n#define D4497_S(x) D4497_S2(x)\n"
                               '#pragma message("D4497 MSC=" D4497_S(_MSC_FULL_VER))\n'
                               '#pragma message("D4497 STL=" D4497_S(_MSVC_STL_UPDATE))\nint main() {}\n')
        msc = re.search(r"D4497 MSC=(\w+)", out)
        stl = re.search(r"D4497 STL=(\w+)", out)
        msvc_version = (f"MSVC _MSC_FULL_VER {msc.group(1) if msc else '?'}, "
                   f"_MSVC_STL_UPDATE {stl.group(1) if stl else '?'} (Compiler Explorer {MSVC_ID})")
        results["toolchains"]["MSVC STL"] = {"version": msvc_version, "flags": MSVC_FLAGS}
        print("[MSVC] feature probes", file=sys.stderr)
        results["features"]["MSVC STL"] = feature_cells(lambda src, name: compile_msvc(src))
        print("[MSVC] integer-class probes", file=sys.stderr)
        cells = {}
        for pid, _label, headers, kind, code, applies in PROBES:
            for t in ("s128c", "u128c"):
                spelling, sign, width, extra = TYPES[t]
                if applies != "both" and applies != sign:
                    continue
                ok, out = compile_msvc(source(headers, kind, code, spelling, width, extra))
                cells[f"{pid}|{t}"] = classify(kind, ok, out, "example.cpp")
                time.sleep(0.5)
        # Whether the compiler has __int128 / _BitInt at all.
        for t, spelling in (("i128", "__int128"), ("b32", "_BitInt(32)")):
            ok, out = compile_msvc(f"{spelling} x = 0;\nint main() {{}}\n")
            cells[f"exists|{t}"] = ("ok" if ok else "rejected", "" if ok else out.splitlines()[0][:200])
        results["msvc_types"] = cells
    return results


def cell(results_for_mode, pid, t, applies, sign):
    if applies != "both" and applies != sign:
        return "—"
    v = results_for_mode.get(f"{pid}|{t}")
    return v[0] if v else "n/a"


def pair_cell(res, pid, s, u, applies):
    a = cell(res, pid, s, applies, "signed")
    b = cell(res, pid, u, applies, "unsigned")
    if applies == "signed":
        return a
    if applies == "unsigned":
        return b
    return a if a == b else f"{a} / {b}"


def markdown(results):
    out = ["# D4497R0 implementation probes", "",
           "Generated by `probes/D4497R0/run_matrix.py`. Do not edit by hand.", "",
           "## Toolchains", "", "| Library | Compiler | Flags |", "|---|---|---|"]
    for tool, info in results["toolchains"].items():
        out.append(f"| {tool} | {info['version']} | `{info['flags']}` |")
    for mode in MODES:
        out += ["", f"## Non-standard integer types, `-std={mode}`", "",
                "Signed / unsigned results are shown as one value when they agree.", ""]
        tools = [t for t in results["toolchains"] if f"{t}|{mode}" in results["types"]]
        head = "| Facility |"
        sep = "|---|"
        for heading, s, u in PAIRS:
            for tool in tools:
                head += f" {heading} {tool} |"
                sep += "---|"
        out += [head, sep]
        for pid, label, _h, _k, _c, applies in PROBES:
            row = f"| {label} |"
            for heading, s, u in PAIRS:
                for tool in tools:
                    res = results["types"][f"{tool}|{mode}"]
                    if f"{pid}|{s}" not in res and f"{pid}|{u}" not in res:
                        row += " n/a |"
                    else:
                        row += f" {pair_cell(res, pid, s, u, applies)} |"
            out.append(row)
    if results.get("msvc_types"):
        out += ["", "## MSVC integer-class types", "", "| Facility | `_Signed128` / `_Unsigned128` |", "|---|---|"]
        for pid, label, _h, _k, _c, applies in PROBES:
            out.append(f"| {label} | {pair_cell(results['msvc_types'], pid, 's128c', 'u128c', applies)} |")
        for t in ("i128", "b32"):
            v = results["msvc_types"].get(f"exists|{t}")
            out.append(f"| `{TYPES[t][0]}` accepted by the compiler | {v[0] if v else 'n/a'} |")
    out += ["", "## Feature availability (standard types, `-std=c++26`)", ""]
    tools = list(results["features"])
    out += ["| Probe | " + " | ".join(tools) + " |", "|---|" + "---|" * len(tools)]
    for fid, label, *_ in FEATURES:
        out.append(f"| {label} | " + " | ".join(results["features"][t][fid][0] for t in tools) + " |")
    for m in MACROS:
        out.append(f"| `{m}` | " + " | ".join(results["features"][t]["macros"][m] for t in tools) + " |")
    out += ["", "## First error message per failing cell", ""]
    for key, cells in list(results["types"].items()) + [("MSVC STL integer-class", results.get("msvc_types", {}))]:
        for k, (val, msg) in sorted(cells.items()):
            if val in ("rejected", "lib-error") and msg:
                out.append(f"- {key} `{k}` ({val}): {msg}")
    return "\n".join(out) + "\n"


# -- tables for the paper ------------------------------------------------------

SYMBOL = {"yes": "yes", "no": "no", "ok": "✓", "rejected": "✗", "lib-error": "✗ lib",
          "no-header": "no header", "n/a": "", "—": ""}
BEGIN = "<!-- BEGIN generated by probes/D4497R0/run_matrix.py -->"
END = "<!-- END generated by probes/D4497R0/run_matrix.py -->"


def sym(v):
    return " / ".join(SYMBOL.get(p.strip(), p.strip()) for p in v.split(" / "))


def same_or_list(values, labels):
    if len(set(values)) == 1:
        return values[0]
    return ", ".join(f"{l}: {v}" for l, v in zip(labels, values))


def paper_tables(results):
    mode = "c++26"
    lcxx = results["types"][f"libc++|{mode}"]
    lstd_gcc = results["types"][f"libstdc++/gcc|{mode}"]
    lstd_clang = results["types"][f"libstdc++/clang|{mode}"]
    out = [BEGIN, ""]
    out += ["Table: How libc++ and libstdc++ treat `__int128` and `_BitInt(N)` "
            "(`-std=c++26`). {#tab:impl-types}", ""]
    out += ["| Facility | `__int128` libc++ | `__int128` libstdc++ | `_BitInt(32)` libc++ "
            "| `_BitInt(128)` libc++ | `_BitInt(256)` libc++ | `_BitInt(N)` libstdc++ |",
            "|:--|:-:|:-:|:-:|:-:|:-:|:-:|"]
    for pid, label, _h, _k, _c, applies in PROBES:
        if pid == "add_sat":
            continue
        cells = [pair_cell(lcxx, pid, "i128", "u128", applies),
                 pair_cell(lstd_gcc, pid, "i128", "u128", applies)]
        for s, u in (("b32", "ub32"), ("b128", "ub128"), ("b256", "ub256")):
            cells.append(pair_cell(lcxx, pid, s, u, applies))
        bit = [pair_cell(lstd_clang, pid, s, u, applies)
               for s, u in (("b32", "ub32"), ("b128", "ub128"), ("b256", "ub256"))]
        cells.append(same_or_list([sym(b) for b in bit], ["32", "128", "256"]))
        out.append(f"| {label} | " + " | ".join(sym(c) if i < 5 else c
                                               for i, c in enumerate(cells)) + " |")
    # Differences between -std=c++26 and -std=gnu++26.
    diffs = []
    for tool in ("libc++", "libstdc++/gcc", "libstdc++/clang"):
        a = results["types"][f"{tool}|c++26"]
        b = results["types"][f"{tool}|gnu++26"]
        for k in sorted(a):
            if a[k][0] != b[k][0]:
                pid, t = k.split("|")
                label = next(p[1] for p in PROBES if p[0] == pid)
                diffs.append(f"{tool.split('/')[0]} {label} with `{TYPES[t][0]}`: "
                             f"{sym(a[k][0])} in `-std=c++26`, {sym(b[k][0])} in `-std=gnu++26`")
    out += ["", "Differences between `-std=c++26` and `-std=gnu++26`: "
            + ("; ".join(sorted(set(diffs))) if diffs else "none") + ".", ""]
    # GCC and Clang agree on __int128 with libstdc++?
    disagree = [k for k in lstd_gcc if lstd_gcc[k][0] != lstd_clang.get(k, ("?",))[0]]
    out += ["Clang and GCC give the same `__int128` results with libstdc++"
            + ("." if not disagree else f", except: {', '.join(disagree)}."), ""]
    if results.get("msvc_types"):
        m = results["msvc_types"]
        accepted = [next(p[1] for p in PROBES if p[0] == pid)
                    for pid, _l, _h, _k, _c, applies in PROBES
                    if pair_cell(m, pid, "s128c", "u128c", applies) in ("ok", "yes")]
        out += ["MSVC rejects both `__int128` and `_BitInt(32)` as types. Of the facilities above, "
                "its integer-class type `std::_Signed128` / `std::_Unsigned128` is accepted only by: "
                + ", ".join(accepted) + ".", ""]
    # Feature availability.
    tools = [t for t in ("libc++", "libstdc++/gcc", "MSVC STL") if t in results["features"]]
    names = {"libc++": "libc++", "libstdc++/gcc": "libstdc++", "MSVC STL": "MSVC STL"}
    out += ["Table: Availability of recent integer facilities with standard types "
            "(`-std=c++26`, `/std:c++latest`). {#tab:impl-features}", ""]
    out += ["| Probe | " + " | ".join(names[t] for t in tools) + " |",
            "|:--|" + ":-:|" * len(tools)]
    for fid, label, *_ in FEATURES:
        out.append(f"| {label} | " + " | ".join(sym(results["features"][t][fid][0]) for t in tools) + " |")
    for mname in MACROS:
        vals = [results["features"][t]["macros"][mname] for t in tools]
        out.append(f"| `{mname}` | " + " | ".join("—" if v == "-" else f"`{v}`" for v in vals) + " |")
    out += ["", END]
    return "\n".join(out)


def update_paper(path, tables):
    text = pathlib.Path(path).read_text()
    if BEGIN not in text or END not in text:
        sys.exit(f"{path}: markers not found")
    head, rest = text.split(BEGIN, 1)
    _old, tail = rest.split(END, 1)
    new = head + tables + tail
    if new != text:
        pathlib.Path(path).write_text(new)
        print(f"updated {path}", file=sys.stderr)
    else:
        print(f"{path} already up to date", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--msvc", action="store_true", help="also query MSVC on Compiler Explorer")
    ap.add_argument("--markdown-only", action="store_true", help="rebuild results.md from results.json")
    ap.add_argument("--update-paper", metavar="PAPER",
                    help="replace the generated block in PAPER with tables from results.json")
    args = ap.parse_args()
    if args.markdown_only or args.update_paper:
        results = json.loads((HERE / "results.json").read_text())
    else:
        results = run(args)
        (HERE / "results.json").write_text(json.dumps(results, indent=1, sort_keys=True) + "\n")
    (HERE / "results.md").write_text(markdown(results))
    print(f"wrote {HERE / 'results.md'}", file=sys.stderr)
    if args.update_paper:
        update_paper(args.update_paper, paper_tables(results))


if __name__ == "__main__":
    main()

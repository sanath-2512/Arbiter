#!/usr/bin/env python3
"""Generate the gauntlet partition of the development eval suite (evalsuite/tasks/g-*).

Forty small repositories across Rust, JavaScript, Python, Go and Ruby, each with a real bug, an
issue written the way users report bugs, visible tests that pass at the base (as in real projects,
they miss the bug), a judge-owned hidden test that fails at the base, and a reference fix.

    python3 scripts/make_gauntlet.py            # (re)write evalsuite/tasks/g-*
    python3 scripts/eval.py --validate-suite    # every judge fails on the base and passes on the reference

The fix is stored as (file, buggy text, fixed text) edits: `reference/` is the base with them applied,
and the scripted offline mode of scripts/eval.py replays them as edit_file calls.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "evalsuite" / "tasks"


def cargo(name: str) -> str:
    return f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n'


def rust(tid, issue, lib, fixes, judge, visible="", extra=None):
    crate = tid.replace("-", "_")
    files = {"Cargo.toml": cargo(crate), "src/lib.rs": lib + ("\n" + visible if visible else "")}
    files.update(extra or {})
    return {"id": tid, "language": "rust", "issue": issue, "files": files, "fixes": fixes,
            "hidden": {"judge.rs": f"use {crate}::*;\n\n" + judge},
            "judge": {"files": {"hidden/judge.rs": "tests/judge.rs"}, "cmd": "cargo test --offline -q --test judge"},
            "policy_test": "cargo test --offline"}


def js(tid, issue, module, src, fixes, judge, visible):
    return {"id": tid, "language": "javascript", "issue": issue,
            "files": {"package.json": json.dumps({"name": tid, "version": "1.0.0", "private": True,
                                                  "scripts": {"test": "node --test"}}, indent=2) + "\n",
                      f"src/{module}.js": src, f"test/{module}.test.js": visible},
            "fixes": fixes, "hidden": {"judge.test.js": judge},
            "judge": {"files": {"hidden": "_judge"}, "cmd": "node --test _judge/judge.test.js"},
            "policy_test": "node --test"}


def py(tid, issue, pkg, src, fixes, judge, visible):
    return {"id": tid, "language": "python", "issue": issue,
            "files": {f"{pkg}/__init__.py": "", f"{pkg}/core.py": src, "tests/__init__.py": "",
                      "tests/test_core.py": visible},
            "fixes": fixes, "hidden": {"__init__.py": "", "test_judge.py": judge},
            "judge": {"files": {"hidden": "_judge"}, "cmd": "python3 -m unittest discover -s _judge -t ."},
            "policy_test": "python3 -m unittest discover -s tests -t ."}


def go(tid, issue, src, fixes, judge, visible):
    return {"id": tid, "language": "go", "issue": issue,
            "files": {"go.mod": f"module example.com/{tid}\n\ngo 1.18\n", "lib.go": src, "lib_test.go": visible},
            "fixes": fixes, "hidden": {"judge_test.go": judge},
            "judge": {"files": {"hidden/judge_test.go": "judge_test.go"}, "cmd": "go test -count=1 -run Judge ./..."},
            "policy_test": "go test ./..."}


def ruby(tid, issue, src, fixes, judge, visible):
    return {"id": tid, "language": "ruby", "issue": issue,
            "files": {"lib/lib.rb": src, "test/test_lib.rb": visible},
            "fixes": fixes, "hidden": {"judge_test.rb": judge},
            "judge": {"files": {"hidden": "_judge"}, "cmd": "ruby -Ilib _judge/judge_test.rb"},
            "policy_test": "ruby -Ilib test/test_lib.rb"}


RS_TESTS = "#[cfg(test)]\nmod tests {{\n    use super::*;\n\n    #[test]\n    fn {name}() {{\n        {body}\n    }}\n}}\n"


def rs_visible(name: str, body: str) -> str:
    return RS_TESTS.format(name=name, body=body)


TASK_LIST = [
    # ------------------------------------------------------------------ Rust
    rust("g-rs-clamp", "`clamp(v, lo, hi)` returns `lo` when `v` is above `hi`. `clamp(15, 0, 10)` gives 0; it should "
         "give 10.",
         "/// Limit `v` to the inclusive range `lo..=hi`.\npub fn clamp(v: i64, lo: i64, hi: i64) -> i64 {\n"
         "    if v < lo {\n        lo\n    } else if v > hi {\n        lo\n    } else {\n        v\n    }\n}\n",
         [("src/lib.rs", "    } else if v > hi {\n        lo\n", "    } else if v > hi {\n        hi\n")],
         "#[test]\nfn judge_above() {\n    assert_eq!(clamp(15, 0, 10), 10);\n    assert_eq!(clamp(-3, 0, 10), 0);\n"
         "    assert_eq!(clamp(7, 0, 10), 7);\n}\n",
         rs_visible("inside", "assert_eq!(clamp(5, 0, 10), 5);")),
    rust("g-rs-median", "`median` of an even number of values returns the upper middle value instead of the mean of "
         "the two middle values: `median(&[1.0, 2.0, 3.0, 4.0])` is 3.0, expected 2.5.",
         "pub fn median(values: &[f64]) -> Option<f64> {\n    if values.is_empty() {\n        return None;\n    }\n"
         "    let mut v = values.to_vec();\n    v.sort_by(|a, b| a.partial_cmp(b).unwrap());\n    let n = v.len();\n"
         "    Some(v[n / 2])\n}\n",
         [("src/lib.rs", "    Some(v[n / 2])\n",
           "    if n % 2 == 0 {\n        Some((v[n / 2 - 1] + v[n / 2]) / 2.0)\n    } else {\n        Some(v[n / 2])\n    }\n")],
         "#[test]\nfn judge_even() {\n    assert_eq!(median(&[4.0, 1.0, 3.0, 2.0]), Some(2.5));\n"
         "    assert_eq!(median(&[5.0, 1.0, 3.0]), Some(3.0));\n    assert_eq!(median(&[]), None);\n}\n",
         rs_visible("odd", "assert_eq!(median(&[3.0, 1.0, 2.0]), Some(2.0));")),
    rust("g-rs-word-count", "`word_count(\"a  b\")` returns 3: runs of spaces, tabs and newlines between words are "
         "counted as extra words.",
         "pub fn word_count(text: &str) -> usize {\n    if text.is_empty() {\n        return 0;\n    }\n"
         "    text.split(' ').count()\n}\n",
         [("src/lib.rs", "    if text.is_empty() {\n        return 0;\n    }\n    text.split(' ').count()\n",
           "    text.split_whitespace().count()\n")],
         "#[test]\nfn judge_whitespace() {\n    assert_eq!(word_count(\"a  b\"), 2);\n"
         "    assert_eq!(word_count(\"  one\\ttwo\\nthree  \"), 3);\n    assert_eq!(word_count(\"   \"), 0);\n}\n",
         rs_visible("simple", "assert_eq!(word_count(\"hello world\"), 2);")),
    rust("g-rs-duration", "`parse_duration(\"1h30m\")` returns 9000 seconds; it should be 5400. Minutes seem to be "
         "counted as hours.",
         "/// Parse durations like \"2h\", \"45m\", \"1h30m\", \"10s\" into seconds.\n"
         "pub fn parse_duration(s: &str) -> Option<u64> {\n    let mut total = 0u64;\n    let mut num = String::new();\n"
         "    for c in s.chars() {\n        if c.is_ascii_digit() {\n            num.push(c);\n            continue;\n"
         "        }\n        let n: u64 = num.parse().ok()?;\n        num.clear();\n        total += match c {\n"
         "            'h' => n * 3600,\n            'm' => n * 3600,\n            's' => n,\n            _ => return None,\n"
         "        };\n    }\n    if !num.is_empty() {\n        return None;\n    }\n    Some(total)\n}\n",
         [("src/lib.rs", "            'm' => n * 3600,\n", "            'm' => n * 60,\n")],
         "#[test]\nfn judge_minutes() {\n    assert_eq!(parse_duration(\"1h30m\"), Some(5400));\n"
         "    assert_eq!(parse_duration(\"45m\"), Some(2700));\n    assert_eq!(parse_duration(\"2h5m10s\"), Some(7510));\n}\n",
         rs_visible("hours", "assert_eq!(parse_duration(\"2h\"), Some(7200));")),
    rust("g-rs-checked-ratio", "`ratio(1, 0)` panics with \"attempt to divide by zero\". It is documented to return "
         "`None` when the divisor is zero.",
         "/// a / b, or None when b is zero.\npub fn ratio(a: i64, b: i64) -> Option<i64> {\n    Some(a / b)\n}\n",
         [("src/lib.rs", "    Some(a / b)\n", "    if b == 0 {\n        None\n    } else {\n        Some(a / b)\n    }\n")],
         "#[test]\nfn judge_zero() {\n    assert_eq!(ratio(1, 0), None);\n    assert_eq!(ratio(9, 3), Some(3));\n}\n",
         rs_visible("plain", "assert_eq!(ratio(10, 2), Some(5));")),
    rust("g-rs-roman", "`to_roman(4)` returns \"IIII\" and `to_roman(1994)` returns \"MDCCCCLXXXXIIII\". Standard "
         "Roman numerals use subtractive forms: IV, IX, XL, XC, CD, CM.",
         "pub fn to_roman(mut n: u32) -> String {\n    let table = [(1000, \"M\"), (500, \"D\"), (100, \"C\"), (50, \"L\"), "
         "(10, \"X\"), (5, \"V\"), (1, \"I\")];\n    let mut out = String::new();\n    for (value, sym) in table {\n"
         "        while n >= value {\n            out.push_str(sym);\n            n -= value;\n        }\n    }\n    out\n}\n",
         [("src/lib.rs", "    let table = [(1000, \"M\"), (500, \"D\"), (100, \"C\"), (50, \"L\"), (10, \"X\"), (5, \"V\"), (1, \"I\")];\n",
           "    let table = [\n        (1000, \"M\"), (900, \"CM\"), (500, \"D\"), (400, \"CD\"), (100, \"C\"), (90, \"XC\"),\n"
           "        (50, \"L\"), (40, \"XL\"), (10, \"X\"), (9, \"IX\"), (5, \"V\"), (4, \"IV\"), (1, \"I\"),\n    ];\n")],
         "#[test]\nfn judge_subtractive() {\n    assert_eq!(to_roman(4), \"IV\");\n    assert_eq!(to_roman(9), \"IX\");\n"
         "    assert_eq!(to_roman(1994), \"MCMXCIV\");\n    assert_eq!(to_roman(3888), \"MMMDCCCLXXXVIII\");\n}\n",
         rs_visible("simple", "assert_eq!(to_roman(3), \"III\");")),
    rust("g-rs-stack-pop", "Calling `pop()` on an empty `Stack` panics (`called Option::unwrap() on a None value`). "
         "It should return `None`.",
         "#[derive(Default)]\npub struct Stack<T> {\n    items: Vec<T>,\n}\n\nimpl<T> Stack<T> {\n"
         "    pub fn new() -> Self {\n        Stack { items: Vec::new() }\n    }\n    pub fn push(&mut self, v: T) {\n"
         "        self.items.push(v);\n    }\n    pub fn pop(&mut self) -> Option<T> {\n"
         "        Some(self.items.pop().unwrap())\n    }\n    pub fn len(&self) -> usize {\n        self.items.len()\n    }\n"
         "    pub fn is_empty(&self) -> bool {\n        self.items.is_empty()\n    }\n}\n",
         [("src/lib.rs", "        Some(self.items.pop().unwrap())\n", "        self.items.pop()\n")],
         "#[test]\nfn judge_empty_pop() {\n    let mut s: Stack<i32> = Stack::new();\n    assert_eq!(s.pop(), None);\n"
         "    s.push(1);\n    assert_eq!(s.pop(), Some(1));\n    assert_eq!(s.pop(), None);\n}\n",
         rs_visible("push_pop", "let mut s = Stack::new(); s.push(2); assert_eq!(s.pop(), Some(2));")),
    rust("g-rs-fahrenheit", "`c_to_f(37.5)` returns 99.0 instead of 99.5: the conversion loses the fraction.",
         "pub fn c_to_f(c: f64) -> f64 {\n    (c as i64 * 9 / 5 + 32) as f64\n}\n",
         [("src/lib.rs", "    (c as i64 * 9 / 5 + 32) as f64\n", "    c * 9.0 / 5.0 + 32.0\n")],
         "#[test]\nfn judge_fraction() {\n    assert!((c_to_f(37.5) - 99.5).abs() < 1e-9);\n"
         "    assert!((c_to_f(-40.0) + 40.0).abs() < 1e-9);\n    assert!((c_to_f(1.0) - 33.8).abs() < 1e-9);\n}\n",
         rs_visible("boiling", "assert_eq!(c_to_f(100.0), 212.0);")),
    rust("g-rs-fizzbuzz", "`fizzbuzz(15)` returns only 14 entries; the last number is missing (should be 1..=n).",
         "pub fn fizzbuzz(n: u32) -> Vec<String> {\n    (1..n)\n        .map(|i| match (i % 3, i % 5) {\n"
         "            (0, 0) => \"FizzBuzz\".to_string(),\n            (0, _) => \"Fizz\".to_string(),\n"
         "            (_, 0) => \"Buzz\".to_string(),\n            _ => i.to_string(),\n        })\n        .collect()\n}\n",
         [("src/lib.rs", "    (1..n)\n", "    (1..=n)\n")],
         "#[test]\nfn judge_inclusive() {\n    let v = fizzbuzz(15);\n    assert_eq!(v.len(), 15);\n"
         "    assert_eq!(v[14], \"FizzBuzz\");\n    assert_eq!(fizzbuzz(1), vec![\"1\".to_string()]);\n}\n",
         rs_visible("fizz", "assert_eq!(fizzbuzz(4)[2], \"Fizz\");")),
    rust("g-rs-unique", "`unique(&[3, 1, 3, 2, 1])` returns `[1, 2, 3]`. It must keep the first occurrence of each "
         "value in the original order: `[3, 1, 2]`.",
         "pub fn unique(values: &[i32]) -> Vec<i32> {\n    let mut v = values.to_vec();\n    v.sort();\n    v.dedup();\n    v\n}\n",
         [("src/lib.rs", "    let mut v = values.to_vec();\n    v.sort();\n    v.dedup();\n    v\n",
           "    let mut seen = std::collections::HashSet::new();\n"
           "    values.iter().copied().filter(|x| seen.insert(*x)).collect()\n")],
         "#[test]\nfn judge_order() {\n    assert_eq!(unique(&[3, 1, 3, 2, 1]), vec![3, 1, 2]);\n"
         "    assert_eq!(unique(&[]), Vec::<i32>::new());\n}\n",
         rs_visible("already_unique", "assert_eq!(unique(&[1, 2]), vec![1, 2]);")),
    rust("g-rs-bank", "`Account::withdraw` lets the balance go negative. Withdrawing more than the balance must "
         "return `Err(Error::InsufficientFunds)` and leave the balance unchanged.",
         "#[derive(Debug, PartialEq)]\npub enum Error {\n    InsufficientFunds,\n}\n\npub struct Account {\n"
         "    pub balance: i64,\n}\n\nimpl Account {\n    pub fn withdraw(&mut self, amount: i64) -> Result<i64, Error> {\n"
         "        self.balance -= amount;\n        Ok(self.balance)\n    }\n}\n",
         [("src/lib.rs", "        self.balance -= amount;\n",
           "        if amount > self.balance {\n            return Err(Error::InsufficientFunds);\n        }\n"
           "        self.balance -= amount;\n")],
         "#[test]\nfn judge_overdraw() {\n    let mut a = Account { balance: 50 };\n"
         "    assert_eq!(a.withdraw(80), Err(Error::InsufficientFunds));\n    assert_eq!(a.balance, 50);\n"
         "    assert_eq!(a.withdraw(50), Ok(0));\n}\n",
         rs_visible("withdraw", "let mut a = Account { balance: 10 }; assert_eq!(a.withdraw(3), Ok(7));")),
    rust("g-rs-fields", "`parse_row(\"a, b ,c\")` returns fields with the spaces kept (`\" b \"`). Fields must be "
         "trimmed.",
         "pub fn parse_row(line: &str) -> Vec<String> {\n    line.split(',').map(|f| f.to_string()).collect()\n}\n",
         [("src/lib.rs", "    line.split(',').map(|f| f.to_string()).collect()\n",
           "    line.split(',').map(|f| f.trim().to_string()).collect()\n")],
         "#[test]\nfn judge_trim() {\n    assert_eq!(parse_row(\"a, b ,c\"), vec![\"a\", \"b\", \"c\"]);\n"
         "    assert_eq!(parse_row(\"  x  \"), vec![\"x\"]);\n}\n",
         rs_visible("plain", "assert_eq!(parse_row(\"a,b\"), vec![\"a\", \"b\"]);")),
    rust("g-rs-version", "Version comparison is lexical: `is_newer(\"1.10.0\", \"1.9.2\")` returns false. Components "
         "must be compared as numbers.",
         "pub mod version;\n\npub use version::is_newer;\n",
         [("src/version.rs", "    a > b\n",
           "    let parse = |s: &str| s.split('.').map(|p| p.parse::<u64>().unwrap_or(0)).collect::<Vec<_>>();\n"
           "    parse(a) > parse(b)\n")],
         "#[test]\nfn judge_numeric() {\n    assert!(is_newer(\"1.10.0\", \"1.9.2\"));\n    assert!(!is_newer(\"1.2.0\", \"1.10.0\"));\n"
         "    assert!(is_newer(\"2.0.0\", \"1.99.99\"));\n}\n",
         extra={"src/version.rs": "/// True when version `a` is newer than version `b` (dotted numbers).\n"
                "pub fn is_newer(a: &str, b: &str) -> bool {\n    a > b\n}\n\n"
                + rs_visible("simple", "assert!(is_newer(\"1.3.0\", \"1.2.0\"));")}),
    rust("g-rs-transpose", "`transpose` of a 2x3 matrix returns a 2x2 result and drops a column.",
         "pub fn transpose(m: &[Vec<i32>]) -> Vec<Vec<i32>> {\n    if m.is_empty() {\n        return vec![];\n    }\n"
         "    let rows = m.len();\n    (0..rows).map(|c| (0..rows).map(|r| m[r][c]).collect()).collect()\n}\n",
         [("src/lib.rs", "    (0..rows).map(|c| (0..rows).map(|r| m[r][c]).collect()).collect()\n",
           "    let cols = m[0].len();\n    (0..cols).map(|c| (0..rows).map(|r| m[r][c]).collect()).collect()\n")],
         "#[test]\nfn judge_rect() {\n    assert_eq!(transpose(&[vec![1, 2, 3], vec![4, 5, 6]]), vec![vec![1, 4], vec![2, 5], vec![3, 6]]);\n}\n",
         rs_visible("square", "assert_eq!(transpose(&[vec![1, 2], vec![3, 4]]), vec![vec![1, 3], vec![2, 4]]);")),
    rust("g-rs-leap", "`is_leap(2000)` returns false, but 2000 was a leap year (years divisible by 400 are leap years).",
         "pub fn is_leap(year: u32) -> bool {\n    year % 4 == 0 && year % 100 != 0\n}\n",
         [("src/lib.rs", "    year % 4 == 0 && year % 100 != 0\n", "    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0\n")],
         "#[test]\nfn judge_400() {\n    assert!(is_leap(2000));\n    assert!(!is_leap(1900));\n    assert!(is_leap(2024));\n}\n",
         rs_visible("common", "assert!(!is_leap(2023));")),
    rust("g-rs-slug", "`slugify(\"Hello,  World!\")` returns \"hello,--world!\". Slugs should contain only lowercase "
         "letters, digits and single dashes: \"hello-world\".",
         "pub fn slugify(s: &str) -> String {\n    s.to_lowercase().replace(' ', \"-\")\n}\n",
         [("src/lib.rs", "    s.to_lowercase().replace(' ', \"-\")\n",
           "    let mut out = String::new();\n    for c in s.to_lowercase().chars() {\n        if c.is_ascii_alphanumeric() {\n"
           "            out.push(c);\n        } else if !out.is_empty() && !out.ends_with('-') {\n            out.push('-');\n"
           "        }\n    }\n    out.trim_end_matches('-').to_string()\n")],
         "#[test]\nfn judge_clean() {\n    assert_eq!(slugify(\"Hello,  World!\"), \"hello-world\");\n"
         "    assert_eq!(slugify(\"  Rust 2024 -- edition \"), \"rust-2024-edition\");\n}\n",
         rs_visible("simple", "assert_eq!(slugify(\"a b\"), \"a-b\");")),
    # ------------------------------------------------------------------ JavaScript
    js("g-js-cents", "`sumPrices([0.1, 0.2])` returns 0.30000000000000004. Money totals must be rounded to cents.",
       "sum", "function sumPrices(prices) {\n  return prices.reduce((a, b) => a + b, 0);\n}\n\nmodule.exports = { sumPrices };\n",
       [("src/sum.js", "  return prices.reduce((a, b) => a + b, 0);\n",
         "  return Math.round(prices.reduce((a, b) => a + b, 0) * 100) / 100;\n")],
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { sumPrices } = require('../src/sum');\n\n"
       "test('judge cents', () => {\n  assert.strictEqual(sumPrices([0.1, 0.2]), 0.3);\n  assert.strictEqual(sumPrices([1.25, 2.1, 0.7]), 4.05);\n});\n",
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { sumPrices } = require('../src/sum');\n\n"
       "test('integers', () => assert.strictEqual(sumPrices([1, 2]), 3));\n"),
    js("g-js-numeric-sort", "`sortNumbers([10, 9, 1])` returns [1, 10, 9]: it sorts as strings.",
       "sort", "function sortNumbers(xs) {\n  return [...xs].sort();\n}\n\nmodule.exports = { sortNumbers };\n",
       [("src/sort.js", "  return [...xs].sort();\n", "  return [...xs].sort((a, b) => a - b);\n")],
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { sortNumbers } = require('../src/sort');\n\n"
       "test('judge numeric', () => {\n  assert.deepStrictEqual(sortNumbers([10, 9, 1]), [1, 9, 10]);\n"
       "  assert.deepStrictEqual(sortNumbers([-2, -10, 3]), [-10, -2, 3]);\n});\n",
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { sortNumbers } = require('../src/sort');\n\n"
       "test('digits', () => assert.deepStrictEqual(sortNumbers([3, 1, 2]), [1, 2, 3]));\n"),
    js("g-js-format-time", "`formatTime(65)` returns \"1:5\"; expected \"01:05\" (minutes and seconds, two digits each).",
       "time", "function formatTime(totalSeconds) {\n  const m = Math.floor(totalSeconds / 60);\n  const s = totalSeconds % 60;\n"
       "  return `${m}:${s}`;\n}\n\nmodule.exports = { formatTime };\n",
       [("src/time.js", "  return `${m}:${s}`;\n", "  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;\n")],
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { formatTime } = require('../src/time');\n\n"
       "test('judge padding', () => {\n  assert.strictEqual(formatTime(65), '01:05');\n  assert.strictEqual(formatTime(0), '00:00');\n"
       "  assert.strictEqual(formatTime(754), '12:34');\n});\n",
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { formatTime } = require('../src/time');\n\n"
       "test('two digits already', () => assert.strictEqual(formatTime(754), '12:34'));\n"),
    js("g-js-deep-equal", "`deepEqual([1, 2], [1, 2, 3])` returns true. Arrays of different lengths are never equal.",
       "equal", "function deepEqual(a, b) {\n  if (a === b) return true;\n  if (typeof a !== 'object' || typeof b !== 'object' || !a || !b) return false;\n"
       "  for (const k of Object.keys(a)) {\n    if (!deepEqual(a[k], b[k])) return false;\n  }\n  return true;\n}\n\nmodule.exports = { deepEqual };\n",
       [("src/equal.js", "  for (const k of Object.keys(a)) {\n",
         "  if (Object.keys(a).length !== Object.keys(b).length) return false;\n  for (const k of Object.keys(a)) {\n")],
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { deepEqual } = require('../src/equal');\n\n"
       "test('judge lengths', () => {\n  assert.strictEqual(deepEqual([1, 2], [1, 2, 3]), false);\n"
       "  assert.strictEqual(deepEqual({ a: 1 }, { a: 1, b: 2 }), false);\n  assert.strictEqual(deepEqual({ a: [1] }, { a: [1] }), true);\n});\n",
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { deepEqual } = require('../src/equal');\n\n"
       "test('same', () => assert.strictEqual(deepEqual([1], [1]), true));\n"),
    js("g-js-chunk", "`chunk([1, 2, 3, 4, 5], 2)` returns [[1, 2], [3, 4]] and loses the 5.",
       "chunk", "function chunk(xs, size) {\n  const out = [];\n  for (let i = 0; i + size <= xs.length; i += size) {\n"
       "    out.push(xs.slice(i, i + size));\n  }\n  return out;\n}\n\nmodule.exports = { chunk };\n",
       [("src/chunk.js", "  for (let i = 0; i + size <= xs.length; i += size) {\n", "  for (let i = 0; i < xs.length; i += size) {\n")],
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { chunk } = require('../src/chunk');\n\n"
       "test('judge remainder', () => {\n  assert.deepStrictEqual(chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]);\n"
       "  assert.deepStrictEqual(chunk([], 3), []);\n});\n",
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { chunk } = require('../src/chunk');\n\n"
       "test('even', () => assert.deepStrictEqual(chunk([1, 2, 3, 4], 2), [[1, 2], [3, 4]]));\n"),
    js("g-js-title-case", "`titleCase('hello   world')` returns 'Hello World': runs of spaces are collapsed. The "
       "original spacing must be preserved ('Hello   World'), and '' must stay ''.",
       "title", "function titleCase(s) {\n  return s.split(' ').filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(' ');\n}\n\n"
       "module.exports = { titleCase };\n",
       [("src/title.js", "  return s.split(' ').filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(' ');\n",
         "  return s.split(' ').map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w)).join(' ');\n")],
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { titleCase } = require('../src/title');\n\n"
       "test('judge spacing', () => {\n  assert.strictEqual(titleCase(''), '');\n"
       "  assert.strictEqual(titleCase('hello   world'), 'Hello   World');\n});\n",
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { titleCase } = require('../src/title');\n\n"
       "test('two words', () => assert.strictEqual(titleCase('a b'), 'A B'));\n"),
    js("g-js-query", "`parseQuery('?q=hello%20world&tag=a+b')` returns raw text: `hello%20world` and `a+b`. Values "
       "must be URL-decoded (`+` is a space).",
       "query", "function parseQuery(qs) {\n  const out = {};\n  for (const part of qs.replace(/^\\?/, '').split('&')) {\n"
       "    if (!part) continue;\n    const [k, v = ''] = part.split('=');\n    out[k] = v;\n  }\n  return out;\n}\n\nmodule.exports = { parseQuery };\n",
       [("src/query.js", "    out[k] = v;\n", "    const dec = (s) => decodeURIComponent(s.replace(/\\+/g, ' '));\n    out[dec(k)] = dec(v);\n")],
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { parseQuery } = require('../src/query');\n\n"
       "test('judge decode', () => {\n  assert.deepStrictEqual(parseQuery('?q=hello%20world&tag=a+b'), { q: 'hello world', tag: 'a b' });\n});\n",
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { parseQuery } = require('../src/query');\n\n"
       "test('plain', () => assert.deepStrictEqual(parseQuery('a=1'), { a: '1' }));\n"),
    js("g-js-range", "`range(1, 5)` returns [1, 2, 3, 4]; the documented behaviour is inclusive: [1, 2, 3, 4, 5].",
       "range", "/** Inclusive range of integers from start to end. */\nfunction range(start, end) {\n  const out = [];\n"
       "  for (let i = start; i < end; i++) out.push(i);\n  return out;\n}\n\nmodule.exports = { range };\n",
       [("src/range.js", "  for (let i = start; i < end; i++) out.push(i);\n", "  for (let i = start; i <= end; i++) out.push(i);\n")],
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { range } = require('../src/range');\n\n"
       "test('judge inclusive', () => {\n  assert.deepStrictEqual(range(1, 5), [1, 2, 3, 4, 5]);\n  assert.deepStrictEqual(range(3, 3), [3]);\n});\n",
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { range } = require('../src/range');\n\n"
       "test('length', () => assert.ok(range(0, 3).length >= 3));\n"),
    js("g-js-retry", "`retry(fn, 3)` calls `fn` only twice before giving up.",
       "retry", "async function retry(fn, attempts) {\n  let last;\n  for (let i = 1; i < attempts; i++) {\n    try {\n"
       "      return await fn();\n    } catch (e) {\n      last = e;\n    }\n  }\n  throw last;\n}\n\nmodule.exports = { retry };\n",
       [("src/retry.js", "  for (let i = 1; i < attempts; i++) {\n", "  for (let i = 0; i < attempts; i++) {\n")],
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { retry } = require('../src/retry');\n\n"
       "test('judge attempts', async () => {\n  let calls = 0;\n  const v = await retry(async () => { calls++; if (calls < 3) throw new Error('x'); return 'ok'; }, 3);\n"
       "  assert.strictEqual(v, 'ok');\n  assert.strictEqual(calls, 3);\n});\n",
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { retry } = require('../src/retry');\n\n"
       "test('succeeds at once', async () => assert.strictEqual(await retry(async () => 1, 3), 1));\n"),
    js("g-js-get-path", "`get({a: {}}, 'a.b.c')` throws a TypeError. Missing intermediate keys should return the "
       "default value (undefined when none is given).",
       "get", "function get(obj, path, dflt) {\n  let cur = obj;\n  for (const key of path.split('.')) {\n    cur = cur[key];\n  }\n"
       "  return cur === undefined ? dflt : cur;\n}\n\nmodule.exports = { get };\n",
       [("src/get.js", "    cur = cur[key];\n", "    if (cur === null || cur === undefined) return dflt;\n    cur = cur[key];\n")],
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { get } = require('../src/get');\n\n"
       "test('judge missing', () => {\n  assert.strictEqual(get({ a: {} }, 'a.b.c'), undefined);\n  assert.strictEqual(get({}, 'x.y', 7), 7);\n"
       "  assert.strictEqual(get({ a: { b: 0 } }, 'a.b', 5), 0);\n});\n",
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { get } = require('../src/get');\n\n"
       "test('present', () => assert.strictEqual(get({ a: { b: 2 } }, 'a.b'), 2));\n"),
    js("g-js-truncate", "`truncate('hello world', 8)` returns 'hello wo...' (11 chars). The result, ellipsis "
       "included, must not exceed the limit: 'hello...'.",
       "truncate", "function truncate(s, max) {\n  if (s.length <= max) return s;\n  return s.slice(0, max) + '...';\n}\n\n"
       "module.exports = { truncate };\n",
       [("src/truncate.js", "  return s.slice(0, max) + '...';\n", "  return s.slice(0, Math.max(0, max - 3)) + '...';\n")],
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { truncate } = require('../src/truncate');\n\n"
       "test('judge limit', () => {\n  assert.strictEqual(truncate('hello world', 8), 'hello...');\n"
       "  assert.ok(truncate('abcdefghijk', 5).length <= 5);\n});\n",
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { truncate } = require('../src/truncate');\n\n"
       "test('short', () => assert.strictEqual(truncate('hi', 5), 'hi'));\n"),
    js("g-js-unique-by", "`uniqueBy(users, 'id')` keeps the last user with each id; it must keep the first one.",
       "unique", "function uniqueBy(items, key) {\n  const m = new Map();\n  for (const it of items) m.set(it[key], it);\n"
       "  return [...m.values()];\n}\n\nmodule.exports = { uniqueBy };\n",
       [("src/unique.js", "  for (const it of items) m.set(it[key], it);\n", "  for (const it of items) if (!m.has(it[key])) m.set(it[key], it);\n")],
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { uniqueBy } = require('../src/unique');\n\n"
       "test('judge first wins', () => {\n  const r = uniqueBy([{ id: 1, n: 'a' }, { id: 2, n: 'b' }, { id: 1, n: 'c' }], 'id');\n"
       "  assert.deepStrictEqual(r.map((x) => x.n), ['a', 'b']);\n});\n",
       "const test = require('node:test');\nconst assert = require('node:assert');\nconst { uniqueBy } = require('../src/unique');\n\n"
       "test('distinct', () => assert.strictEqual(uniqueBy([{ id: 1 }, { id: 2 }], 'id').length, 2));\n"),
    # ------------------------------------------------------------------ Python
    py("g-py-median", "`median([1, 2, 3, 4])` returns 3 instead of 2.5.", "stats",
       "def median(values):\n    v = sorted(values)\n    return v[len(v) // 2]\n",
       [("stats/core.py", "    return v[len(v) // 2]\n",
         "    n = len(v)\n    if n % 2 == 0:\n        return (v[n // 2 - 1] + v[n // 2]) / 2\n    return v[n // 2]\n")],
       "import unittest\n\nfrom stats.core import median\n\n\nclass Judge(unittest.TestCase):\n    def test_even(self):\n"
       "        self.assertEqual(median([4, 1, 3, 2]), 2.5)\n        self.assertEqual(median([3, 1, 2]), 2)\n",
       "import unittest\n\nfrom stats.core import median\n\n\nclass T(unittest.TestCase):\n    def test_odd(self):\n"
       "        self.assertEqual(median([1, 2, 3]), 2)\n"),
    py("g-py-chunks", "`chunks([1, 2, 3, 4, 5], 2)` drops the trailing 5.", "util",
       "def chunks(xs, n):\n    return [xs[i:i + n] for i in range(0, len(xs) - n + 1, n)]\n",
       [("util/core.py", "    return [xs[i:i + n] for i in range(0, len(xs) - n + 1, n)]\n",
         "    return [xs[i:i + n] for i in range(0, len(xs), n)]\n")],
       "import unittest\n\nfrom util.core import chunks\n\n\nclass Judge(unittest.TestCase):\n    def test_tail(self):\n"
       "        self.assertEqual(chunks([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])\n",
       "import unittest\n\nfrom util.core import chunks\n\n\nclass T(unittest.TestCase):\n    def test_even(self):\n"
       "        self.assertEqual(chunks([1, 2], 1), [[1], [2]])\n"),
    py("g-py-safe-int", "`to_int(' 42 ')` raises ValueError and `to_int('x')` crashes. Surrounding whitespace should "
       "be accepted, and invalid input should return the default (None).", "conv",
       "def to_int(s, default=None):\n    return int(s)\n",
       [("conv/core.py", "    return int(s)\n", "    try:\n        return int(str(s).strip())\n    except ValueError:\n        return default\n")],
       "import unittest\n\nfrom conv.core import to_int\n\n\nclass Judge(unittest.TestCase):\n    def test_lenient(self):\n"
       "        self.assertEqual(to_int(' 42 '), 42)\n        self.assertIsNone(to_int('x'))\n        self.assertEqual(to_int('x', 0), 0)\n",
       "import unittest\n\nfrom conv.core import to_int\n\n\nclass T(unittest.TestCase):\n    def test_plain(self):\n"
       "        self.assertEqual(to_int('7'), 7)\n"),
    py("g-py-flatten", "`flatten([1, [2, [3, [4]]]])` returns [1, 2, [3, [4]]]; nested lists at any depth should be "
       "flattened.", "lists",
       "def flatten(xs):\n    out = []\n    for x in xs:\n        if isinstance(x, list):\n            out.extend(x)\n"
       "        else:\n            out.append(x)\n    return out\n",
       [("lists/core.py", "            out.extend(x)\n", "            out.extend(flatten(x))\n")],
       "import unittest\n\nfrom lists.core import flatten\n\n\nclass Judge(unittest.TestCase):\n    def test_deep(self):\n"
       "        self.assertEqual(flatten([1, [2, [3, [4]]]]), [1, 2, 3, 4])\n",
       "import unittest\n\nfrom lists.core import flatten\n\n\nclass T(unittest.TestCase):\n    def test_one_level(self):\n"
       "        self.assertEqual(flatten([1, [2]]), [1, 2])\n"),
    py("g-py-percent", "`percent(1, 3)` returns '33.0%'; it should round to one decimal: '33.3%'. And percent(0, 0) "
       "crashes; it should return '0.0%'.", "fmt",
       "def percent(part, total):\n    return f'{part * 100 // total:.1f}%'\n",
       [("fmt/core.py", "    return f'{part * 100 // total:.1f}%'\n",
         "    if total == 0:\n        return '0.0%'\n    return f'{part * 100 / total:.1f}%'\n")],
       "import unittest\n\nfrom fmt.core import percent\n\n\nclass Judge(unittest.TestCase):\n    def test_round(self):\n"
       "        self.assertEqual(percent(1, 3), '33.3%')\n        self.assertEqual(percent(0, 0), '0.0%')\n",
       "import unittest\n\nfrom fmt.core import percent\n\n\nclass T(unittest.TestCase):\n    def test_half(self):\n"
       "        self.assertEqual(percent(1, 2), '50.0%')\n"),
    py("g-py-merge-intervals", "`merge([(1, 3), (2, 6), (8, 10)])` returns the input unchanged when intervals are not "
       "sorted by start, e.g. `merge([(8, 10), (1, 3), (2, 6)])`.", "intervals",
       "def merge(intervals):\n    out = []\n    for s, e in intervals:\n        if out and s <= out[-1][1]:\n"
       "            out[-1] = (out[-1][0], max(out[-1][1], e))\n        else:\n            out.append((s, e))\n    return out\n",
       [("intervals/core.py", "    for s, e in intervals:\n", "    for s, e in sorted(intervals):\n")],
       "import unittest\n\nfrom intervals.core import merge\n\n\nclass Judge(unittest.TestCase):\n    def test_unsorted(self):\n"
       "        self.assertEqual(merge([(8, 10), (1, 3), (2, 6)]), [(1, 6), (8, 10)])\n",
       "import unittest\n\nfrom intervals.core import merge\n\n\nclass T(unittest.TestCase):\n    def test_sorted(self):\n"
       "        self.assertEqual(merge([(1, 3), (2, 6)]), [(1, 6)])\n"),
    # ------------------------------------------------------------------ Go
    go("g-go-reverse", "`Reverse(\"héllo\")` returns garbled text: it reverses bytes, not characters.",
       "package lib\n\n// Reverse returns s with its characters in reverse order.\nfunc Reverse(s string) string {\n"
       "\tb := []byte(s)\n\tfor i, j := 0, len(b)-1; i < j; i, j = i+1, j-1 {\n\t\tb[i], b[j] = b[j], b[i]\n\t}\n\treturn string(b)\n}\n",
       [("lib.go", "\tb := []byte(s)\n", "\tb := []rune(s)\n")],
       "package lib\n\nimport \"testing\"\n\nfunc TestJudgeUnicode(t *testing.T) {\n\tif got := Reverse(\"héllo\"); got != \"olléh\" {\n"
       "\t\tt.Fatalf(\"got %q\", got)\n\t}\n}\n",
       "package lib\n\nimport \"testing\"\n\nfunc TestASCII(t *testing.T) {\n\tif Reverse(\"ab\") != \"ba\" {\n\t\tt.Fatal(\"ascii\")\n\t}\n}\n"),
    go("g-go-max", "`Max(nil)` panics with an index out of range. For an empty slice it should return 0 and false.",
       "package lib\n\n// Max returns the largest value and whether the slice was non-empty.\nfunc Max(xs []int) (int, bool) {\n"
       "\tm := xs[0]\n\tfor _, x := range xs {\n\t\tif x > m {\n\t\t\tm = x\n\t\t}\n\t}\n\treturn m, true\n}\n",
       [("lib.go", "\tm := xs[0]\n", "\tif len(xs) == 0 {\n\t\treturn 0, false\n\t}\n\tm := xs[0]\n")],
       "package lib\n\nimport \"testing\"\n\nfunc TestJudgeEmpty(t *testing.T) {\n\tif m, ok := Max(nil); ok || m != 0 {\n"
       "\t\tt.Fatalf(\"got %d %v\", m, ok)\n\t}\n}\n",
       "package lib\n\nimport \"testing\"\n\nfunc TestMax(t *testing.T) {\n\tif m, _ := Max([]int{1, 5, 2}); m != 5 {\n\t\tt.Fatal(m)\n\t}\n}\n"),
    go("g-go-sum-positive", "`SumPositive([]int{-1, 2, 3})` returns 4: negative numbers are subtracted instead of "
       "skipped.",
       "package lib\n\n// SumPositive adds the values greater than zero.\nfunc SumPositive(xs []int) int {\n\ttotal := 0\n"
       "\tfor _, x := range xs {\n\t\ttotal += x\n\t}\n\treturn total\n}\n",
       [("lib.go", "\t\ttotal += x\n", "\t\tif x > 0 {\n\t\t\ttotal += x\n\t\t}\n")],
       "package lib\n\nimport \"testing\"\n\nfunc TestJudgeNegative(t *testing.T) {\n\tif got := SumPositive([]int{-1, 2, 3}); got != 5 {\n"
       "\t\tt.Fatalf(\"got %d\", got)\n\t}\n}\n",
       "package lib\n\nimport \"testing\"\n\nfunc TestPositive(t *testing.T) {\n\tif SumPositive([]int{1, 2}) != 3 {\n\t\tt.Fatal(\"sum\")\n\t}\n}\n"),
    go("g-go-initials", "`Initials(\"ada  lovelace\")` returns \"A L\" with a stray space for the double space; "
       "expected \"AL\".",
       "package lib\n\nimport \"strings\"\n\n// Initials returns the upper-case first letter of each word.\nfunc Initials(name string) string {\n"
       "\tvar b strings.Builder\n\tfor _, w := range strings.Split(name, \" \") {\n\t\tif w == \"\" {\n\t\t\tb.WriteString(\" \")\n"
       "\t\t\tcontinue\n\t\t}\n\t\tb.WriteString(strings.ToUpper(w[:1]))\n\t}\n\treturn b.String()\n}\n",
       [("lib.go", "\tfor _, w := range strings.Split(name, \" \") {\n\t\tif w == \"\" {\n\t\t\tb.WriteString(\" \")\n\t\t\tcontinue\n\t\t}\n",
         "\tfor _, w := range strings.Fields(name) {\n")],
       "package lib\n\nimport \"testing\"\n\nfunc TestJudgeSpaces(t *testing.T) {\n\tif got := Initials(\"ada  lovelace\"); got != \"AL\" {\n"
       "\t\tt.Fatalf(\"got %q\", got)\n\t}\n}\n",
       "package lib\n\nimport \"testing\"\n\nfunc TestInitials(t *testing.T) {\n\tif Initials(\"grace hopper\") != \"GH\" {\n\t\tt.Fatal(\"gh\")\n\t}\n}\n"),
    # ------------------------------------------------------------------ Ruby
    ruby("g-rb-average", "`Stats.average([1, 2])` returns 1 instead of 1.5.",
         "module Stats\n  def self.average(xs)\n    return 0 if xs.empty?\n\n    xs.sum / xs.size\n  end\nend\n",
         [("lib/lib.rb", "    xs.sum / xs.size\n", "    xs.sum.to_f / xs.size\n")],
         "require 'minitest/autorun'\nrequire 'lib'\n\nclass JudgeTest < Minitest::Test\n  def test_fraction\n"
         "    assert_equal 1.5, Stats.average([1, 2])\n  end\nend\n",
         "require 'minitest/autorun'\nrequire 'lib'\n\nclass StatsTest < Minitest::Test\n  def test_empty\n"
         "    assert_equal 0, Stats.average([])\n  end\nend\n"),
    ruby("g-rb-pluralize", "`Words.pluralize(\"box\", 2)` returns \"boxs\"; words ending in s, x, z, ch or sh take \"es\".",
         "module Words\n  def self.pluralize(word, n)\n    return word if n == 1\n\n    word + 's'\n  end\nend\n",
         [("lib/lib.rb", "    word + 's'\n", "    word.match?(/(s|x|z|ch|sh)\\z/) ? word + 'es' : word + 's'\n")],
         "require 'minitest/autorun'\nrequire 'lib'\n\nclass JudgeTest < Minitest::Test\n  def test_es\n"
         "    assert_equal 'boxes', Words.pluralize('box', 2)\n    assert_equal 'churches', Words.pluralize('church', 3)\n"
         "    assert_equal 'cats', Words.pluralize('cat', 2)\n  end\nend\n",
         "require 'minitest/autorun'\nrequire 'lib'\n\nclass WordsTest < Minitest::Test\n  def test_one\n"
         "    assert_equal 'cat', Words.pluralize('cat', 1)\n  end\nend\n"),
]


def write(task: dict) -> None:
    d = TASKS / task["id"]
    if d.exists():
        shutil.rmtree(d)
    for rel, text in task["files"].items():
        (d / "repo" / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / "repo" / rel).write_text(text)
    for rel, text in task["hidden"].items():
        (d / "hidden" / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / "hidden" / rel).write_text(text)
    for path, buggy, fixed in task["fixes"]:
        src = (d / "reference" / path) if (d / "reference" / path).exists() else (d / "repo" / path)
        text = src.read_text()
        if text.count(buggy) != 1:
            raise SystemExit(f"{task['id']}: buggy text not found exactly once in {path}")
        (d / "reference" / path).parent.mkdir(parents=True, exist_ok=True)
        (d / "reference" / path).write_text(text.replace(buggy, fixed))
    (d / "issue.md").write_text(task["issue"] + "\n")
    spec = {"task_id": task["id"], "partition": "gauntlet", "kind": "regression", "language": task["language"],
            "limits": {"time_limit_s": 900, "max_steps": 40}, "judge": task["judge"],
            "scripted": {"edits": [{"path": p, "old_str": b, "new_str": f} for p, b, f in task["fixes"]],
                         "test": task["policy_test"]}}
    (d / "task.json").write_text(json.dumps(spec, indent=1) + "\n")


def main() -> None:
    ids = [t["id"] for t in TASK_LIST]
    assert len(ids) == len(set(ids)), "duplicate task ids"
    for t in TASK_LIST:
        write(t)
    by_lang: dict[str, int] = {}
    for t in TASK_LIST:
        by_lang[t["language"]] = by_lang.get(t["language"], 0) + 1
    print(f"wrote {len(TASK_LIST)} tasks: {by_lang}")


if __name__ == "__main__":
    main()

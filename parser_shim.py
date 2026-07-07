"""Compatibility shim for the removed `parser` module.

DS-1000 test_code.py files use parser.suite() and parser.st2list() to tokenize
generated code and check for forbidden constructs (e.g. for/while loops).
The `parser` module was removed in Python 3.12 (deprecated since 3.9).

This shim provides just enough of the interface to satisfy DS-1000's usage:
    ast_obj = parser.suite(code)
    token_tree = parser.st2list(ast_obj)
    leaves = extract_element(token_tree)   # DS-1000's helper to flatten
    "for" not in leaves                     # typical check
"""

import tokenize
import io


class _ST:
    def __init__(self, code):
        self.code = code


def suite(code):
    return _ST(code)


def st2list(st):
    tokens = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(st.code).readline):
            if tok.string:
                tokens.append(tok.string)
    except tokenize.TokenizeError:
        pass
    return tokens

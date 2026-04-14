# CI fixer e2e test — intentional ruff violations
import os,sys,json  # noqa: E401 multiple imports on one line
x=1+2  # noqa: E225 missing whitespace around operator — but we want ruff to catch it
unused_var = "this variable is never used"  # F841
def badly_formatted_function(a,b,c):  # E231 missing whitespace after ','
    return a+b+c  # E225

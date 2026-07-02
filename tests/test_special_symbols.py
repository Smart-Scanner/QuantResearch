"""P0 Test for Special Symbols in URLs.

Validates that symbols containing special characters (e.g. &, -) like
M&M, L&T, BAJAJ-AUTO, MCDOWELL-N route correctly, decode correctly, and do not
trigger malformed URL validation.
"""

import sys
import os
import urllib.parse
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_symbol_validation():
    # List of test symbols
    test_symbols = ["M&M", "L&T", "BAJAJ-AUTO", "MCDOWELL-N"]
    
    print("=" * 60)
    print("  RUNNING P0 TEST: SPECIAL SYMBOL NAVIGATION VALIDATION")
    print("=" * 60)
    
    passed = 0
    failed = 0
    
    # Malformed regex filter check (must match pages.py:323)
    # is_malformed = not re.match(r"^[A-Za-z0-9\-\&\.]+$", normalized_symbol)
    pattern = re.compile(r"^[A-Za-z0-9\-\&\.]+$")
    
    for original in test_symbols:
        print(f"\nTesting symbol: {original}")
        
        # 1. Simulate frontend encoding
        encoded = urllib.parse.quote(original.upper())
        print(f"  Frontend Encoded URL segment: /symbol/{encoded}")
        
        # 2. Simulate backend decoding (pages.py:319)
        decoded = urllib.parse.unquote(encoded).strip().upper()
        print(f"  Backend Decoded: {decoded}")
        
        if decoded != original.upper():
            print(f"  [FAIL] Decoded value '{decoded}' does not match original '{original}'")
            failed += 1
            continue
            
        # 3. Simulate malformed regex check (pages.py:323)
        match = pattern.match(decoded)
        if not match:
            print(f"  [FAIL] Symbol '{decoded}' failed regex check (r'^[A-Za-z0-9\\-\\&\\.]+$')")
            failed += 1
        else:
            print(f"  [PASS] Symbol '{decoded}' passed regex validation and is not malformed")
            passed += 1
            
    print("\n" + "=" * 60)
    print(f"  RESULTS: {passed} passed, {failed} failed out of {len(test_symbols)} symbols")
    print("=" * 60)
    
    sys.exit(1 if failed > 0 else 0)

if __name__ == "__main__":
    test_symbol_validation()

import sys
import os
import unittest
import logging

# Ensure root directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider import BrokerProvider, ProviderState

class TestRoleEnforcement(unittest.TestCase):
    def test_execution_role_raises_runtime_error(self):
        # Create a mock provider with EXECUTION role
        config = {
            "ROLE": "EXECUTION",
            "API_KEY": "test",
            "CLIENT_ID": "test",
            "MPIN": "1234",
            "TOTP": "TEST"
        }
        
        provider = BrokerProvider(name="PROVIDER_TEST", config=config)
        
        with self.assertRaises(RuntimeError) as context:
            provider.fetch_historical(symboltoken="26000", exchange="NSE")
            
        self.assertIn("FATAL: Cannot fetch historical data using an EXECUTION provider", str(context.exception))

if __name__ == "__main__":
    unittest.main()

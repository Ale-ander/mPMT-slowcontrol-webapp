#!/usr/bin/env python3

def decode_status(status_code: int) -> str:
    """Convert numeric HV status code to human-readable string."""
    mapping = {
        0: 'UP', 1: 'DOWN', 2: 'RUP', 3: 'RDN',
        4: 'TUP', 5: 'TDN', 6: 'TRIP', -1: 'ERR'
    }
    return mapping.get(status_code, 'undef')

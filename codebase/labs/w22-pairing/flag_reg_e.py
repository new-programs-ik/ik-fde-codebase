from datetime import date

# Reg E: provisional credit must post within 10 BUSINESS days of the dispute being filed.
# Return the dispute_ids that are BREACHED:
#   more than 10 business days (Mon-Fri) elapsed between filed_date and TODAY,
#   AND status is not 'resolved'.
#
# The CSV is a real-world messy export. Inspect it before you trust it.

TODAY = date(2026, 6, 29)  # hardcode for deterministic grading

def flag_reg_e(path: str) -> list:
    # drive your AI assistant to implement this.
    ...

if __name__ == "__main__":
    print(flag_reg_e("disputes_messy.csv"))

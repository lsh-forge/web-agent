import re
def is_binary_data(data)-> bool:
    # Count occurrences of \u sequences (handling both complete and incomplete sequences)
    try:
        # 正确的正则：匹配 \xXX 或 \uXXXX 或 \UXXXXXXXX
        pattern = r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]'
        # Look for both complete (4 hex chars) and incomplete \u sequences
        complete_unicode_matches = re.findall(pattern, data)

        # Count total \u sequences (both complete and incomplete)
        total_matches = len(complete_unicode_matches)

        if total_matches >= 10:
            return True
    except Exception:
        # If regex fails for any reason, fall back to a simpler check
        try:
            if data.count('\\u') >= 10:
                return True
        except Exception:
            # If even the simple check fails, return original data
            pass
    return False
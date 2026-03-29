"""EMVCo QR Code generator for Argentina Transferencias 3.0.

Generates interoperable QR codes compatible with Mercado Pago, MODO,
and other Argentine wallet apps that support the Transferencias 3.0 standard.

The QR contains structured TLV (Tag-Length-Value) data following the
EMVCo Merchant-Presented QR specification.
"""

from datetime import datetime, timezone, timedelta

# Argentina timezone (UTC-3)
_ART = timezone(timedelta(hours=-3))

def _crc16_ccitt(data):
    """Calculate CRC-16/CCITT-FALSE checksum (EMVCo standard)."""
    crc = 0xFFFF
    for byte in data.encode('utf-8') if isinstance(data, str) else data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return format(crc, '04X')


def _tlv(tag, value):
    """Build a single TLV field: tag(2) + length(2) + value."""
    return f'{tag}{len(value):02d}{value}'


def parse_emvco(raw):
    """Parse EMVCo QR string into an ordered list of (tag, value) tuples.

    Also parses sub-TLV inside compound tags (02-51, 62, 64).
    Returns dict with tag as key, value as string (or dict for compound tags).
    """
    result = {}
    pos = 0
    while pos + 4 <= len(raw):
        tag = raw[pos:pos + 2]
        length_str = raw[pos + 2:pos + 4]
        if not length_str.isdigit():
            break
        length = int(length_str)
        value = raw[pos + 4:pos + 4 + length]
        if len(value) < length:
            break

        # Compound tags (Merchant Account Info 02-51, Additional Data 62,
        # Language 64, Unreserved Templates 80-99)
        tag_num = int(tag)
        if (2 <= tag_num <= 51) or tag in ('62', '64') or (80 <= tag_num <= 99):
            sub = {}
            spos = 0
            while spos + 4 <= len(value):
                stag = value[spos:spos + 2]
                slen_str = value[spos + 2:spos + 4]
                if not slen_str.isdigit():
                    break
                slen = int(slen_str)
                sval = value[spos + 4:spos + 4 + slen]
                if len(sval) < slen:
                    break
                sub[stag] = sval
                spos += 4 + slen
            result[tag] = {'_raw': value, '_sub': sub}
        else:
            result[tag] = value

        pos += 4 + length
    return result


def build_emvco(parsed, amount=None, reference=None):
    """Build an EMVCo QR string from parsed data, optionally overriding
    amount (Tag 54) and adding a reference (Tag 62 sub-tag 05).

    Args:
        parsed: dict from parse_emvco()
        amount: decimal amount as string (e.g. "15.00") or None to keep original
        reference: transaction reference string or None to keep original

    Returns:
        Complete EMVCo QR string with recalculated CRC.
    """
    # Ensure dynamic QR
    tags = dict(parsed)
    tags['01'] = '12'  # Point of Initiation = Dynamic

    if amount is not None:
        tags['54'] = str(amount)

    if reference is not None:
        # Build Tag 62 (Additional Data) with our reference
        existing_62 = tags.get('62', {})
        if isinstance(existing_62, dict):
            subs = dict(existing_62.get('_sub', {}))
        else:
            subs = {}
        subs['05'] = reference  # Sub-tag 05 = Reference Label
        # Rebuild Tag 62 value from sub-tags
        tag62_val = ''
        for stag in sorted(subs.keys()):
            tag62_val += _tlv(stag, subs[stag])
        tags['62'] = {'_raw': tag62_val, '_sub': subs}

    # Update timestamp in Tag 80 (Fiserv custom data, sub-tag 03)
    # Format: YYMMDDHHmmSS in Argentina time (UTC-3)
    existing_80 = tags.get('80')
    if isinstance(existing_80, dict) and '03' in existing_80.get('_sub', {}):
        now = datetime.now(_ART)
        timestamp = now.strftime('%y%m%d%H%M%S')
        subs_80 = dict(existing_80.get('_sub', {}))
        subs_80['03'] = timestamp
        tag80_val = ''
        for stag in sorted(subs_80.keys()):
            tag80_val += _tlv(stag, subs_80[stag])
        tags['80'] = {'_raw': tag80_val, '_sub': subs_80}
    elif isinstance(existing_80, str):
        # Tag 80 wasn't parsed as compound — try to find and replace
        # the 12-char timestamp pattern (sub-tag 03, len 12)
        now = datetime.now(_ART)
        timestamp = now.strftime('%y%m%d%H%M%S')
        idx = existing_80.find('0312')
        if idx >= 0:
            tags['80'] = existing_80[:idx + 4] + timestamp + existing_80[idx + 16:]

    # Build QR string in tag order (00 first, 63 last)
    # Standard tag order: 00, 01, 02-51, 52, 53, 54, 55-58, 59, 60, 61, 62, 64, 63
    qr = ''
    tag_order = []

    # Collect all tags except 63 (CRC)
    for tag in sorted(tags.keys()):
        if tag == '63':
            continue
        tag_order.append(tag)

    for tag in tag_order:
        val = tags[tag]
        if isinstance(val, dict):
            # Compound tag — use _raw or rebuild from _sub
            raw_val = val.get('_raw', '')
            if not raw_val and '_sub' in val:
                for stag in sorted(val['_sub'].keys()):
                    raw_val += _tlv(stag, val['_sub'][stag])
            qr += _tlv(tag, raw_val)
        else:
            qr += _tlv(tag, val)

    # Append CRC placeholder and calculate
    qr += '6304'
    crc = _crc16_ccitt(qr)
    qr += crc

    return qr


def validate_template(raw):
    """Validate an EMVCo QR template string.

    Returns (is_valid, error_message).
    """
    if not raw or len(raw) < 20:
        return False, 'Template is too short.'
    if not raw.startswith('0002'):
        return False, 'Template must start with "0002" (EMVCo format indicator).'
    # CRC check: last 4 chars should match CRC of everything before them
    content = raw[:-4]
    expected_crc = _crc16_ccitt(content)
    actual_crc = raw[-4:]
    if expected_crc.upper() != actual_crc.upper():
        return False, (
            f'CRC mismatch: expected {expected_crc}, got {actual_crc}. '
            'Check that the template text was copied exactly '
            '(including spaces in the merchant name).'
        )
    parsed = parse_emvco(raw)
    if '43' not in parsed:
        return False, 'Missing tag 43 (Merchant Account Information).'
    if '59' not in parsed:
        return False, 'Missing tag 59 (Merchant Name).'
    return True, ''


def generate_payment_qr(template_raw, amount_str, reference):
    """High-level: generate a payment QR from a stored template.

    Args:
        template_raw: raw EMVCo string from the device QR (stored in DB)
        amount_str: amount as string with decimals (e.g. "15.00")
        reference: unique transaction reference (e.g. Clover order ID)

    Returns:
        Complete EMVCo QR string ready to encode as QR image.
    """
    parsed = parse_emvco(template_raw)
    return build_emvco(parsed, amount=amount_str, reference=reference)

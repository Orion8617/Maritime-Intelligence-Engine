import math
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

AIS_CHAR_TABLE = "0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVW`abcdefghijklmnopqrstuvw"

NAV_STATUS_CODES = {
    0: 'Under way using engine',
    1: 'At anchor',
    2: 'Not under command',
    3: 'Restricted manoeuvrability',
    4: 'Constrained by draught',
    5: 'Moored',
    6: 'Aground',
    7: 'Engaged in fishing',
    8: 'Under way sailing',
    9: 'Reserved (HSC)',
    10: 'Reserved (WIG)',
    11: 'Power-driven vessel towing astern',
    12: 'Power-driven vessel pushing ahead',
    13: 'Reserved',
    14: 'AIS-SART active',
    15: 'Not defined',
}

SHIP_TYPE_CODES = {
    0: 'Not available',
    20: 'Wing in ground', 21: 'WIG - Hazardous A', 22: 'WIG - Hazardous B',
    23: 'WIG - Hazardous C', 24: 'WIG - Hazardous D',
    30: 'Fishing', 31: 'Towing', 32: 'Towing (large)', 33: 'Dredging/underwater ops',
    34: 'Diving ops', 35: 'Military ops', 36: 'Sailing', 37: 'Pleasure craft',
    40: 'High speed craft', 41: 'HSC - Hazardous A', 42: 'HSC - Hazardous B',
    43: 'HSC - Hazardous C', 44: 'HSC - Hazardous D', 49: 'HSC - No info',
    50: 'Pilot vessel', 51: 'Search and rescue', 52: 'Tug',
    53: 'Port tender', 54: 'Anti-pollution', 55: 'Law enforcement',
    56: 'Spare local', 57: 'Spare local', 58: 'Medical transport', 59: 'Noncombatant',
    60: 'Passenger', 61: 'Passenger - Hazardous A', 62: 'Passenger - Hazardous B',
    63: 'Passenger - Hazardous C', 64: 'Passenger - Hazardous D',
    69: 'Passenger - No info',
    70: 'Cargo', 71: 'Cargo - Hazardous A', 72: 'Cargo - Hazardous B',
    73: 'Cargo - Hazardous C', 74: 'Cargo - Hazardous D', 79: 'Cargo - No info',
    80: 'Tanker', 81: 'Tanker - Hazardous A', 82: 'Tanker - Hazardous B',
    83: 'Tanker - Hazardous C', 84: 'Tanker - Hazardous D', 89: 'Tanker - No info',
    90: 'Other', 91: 'Other - Hazardous A', 92: 'Other - Hazardous B',
    93: 'Other - Hazardous C', 94: 'Other - Hazardous D', 99: 'Other - No info',
}

EPFD_CODES = {
    0: 'Undefined', 1: 'GPS', 2: 'GLONASS', 3: 'GPS+GLONASS',
    4: 'Loran-C', 5: 'Chayka', 6: 'Integrated navigation',
    7: 'Surveyed', 8: 'Galileo', 15: 'Internal GNSS',
}


def _char_to_6bit(c):
    v = ord(c) - 48
    if v > 40:
        v -= 8
    return v & 0x3F


class _AISBits:
    """Bit-vector backed by a single Python big-int (MSB-first).
    O(1) field extraction vs O(N) of the legacy list-of-bits implementation.
    Keeps __getitem__/__len__ for any legacy caller that indexes directly."""
    __slots__ = ('val', 'length')

    def __init__(self, val, length):
        self.val = val
        self.length = length

    def __len__(self):
        return self.length

    def __getitem__(self, i):
        if not 0 <= i < self.length:
            return 0
        return (self.val >> (self.length - 1 - i)) & 1


def _payload_to_bits(payload, fill_bits=0):
    val = 0
    n = 0
    for c in payload:
        v = ord(c) - 48
        if v > 40:
            v -= 8
        val = (val << 6) | (v & 0x3F)
        n += 6
    if fill_bits > 0 and fill_bits < n:
        val >>= fill_bits
        n -= fill_bits
    return _AISBits(val, n)


def _bits_to_uint(bits, start, length):
    if isinstance(bits, _AISBits):
        if start >= bits.length or length <= 0:
            return 0
        avail = bits.length - start
        if length <= avail:
            shift = avail - length
            return (bits.val >> shift) & ((1 << length) - 1)
        # Pad missing trailing bits with zeros (matches legacy behavior of shifting in 0s).
        partial = bits.val & ((1 << avail) - 1)
        return partial << (length - avail)
    # Legacy list-of-bits fallback.
    val = 0
    blen = len(bits)
    for i in range(length):
        idx = start + i
        if idx < blen:
            val = (val << 1) | bits[idx]
        else:
            val = val << 1
    return val


def _bits_to_int(bits, start, length):
    val = _bits_to_uint(bits, start, length)
    if val & (1 << (length - 1)):
        val -= (1 << length)
    return val


def _bits_to_string(bits, start, length_bits):
    chars = []
    for i in range(0, length_bits, 6):
        idx = start + i
        if idx + 6 <= len(bits):
            c = _bits_to_uint(bits, idx, 6)
        else:
            break
        if c == 0:
            chars.append(' ')
        elif 1 <= c <= 31:
            chars.append(chr(c + 64))
        elif 32 <= c <= 63:
            chars.append(chr(c))
        else:
            chars.append('?')
    return ''.join(chars).strip().rstrip('@').strip()


def parse_nmea_sentence(line):
    line = line.strip()
    if not line:
        return None

    if line[0] == '\\':
        tag_end = line.find('\\', 1)
        if tag_end > 0:
            line = line[tag_end + 1:]

    checksum_split = line.rsplit('*', 1)
    body = checksum_split[0]

    if body.startswith('!'):
        body = body[1:]

    parts = body.split(',')
    if len(parts) < 7:
        return None

    sentence_type = parts[0]
    if sentence_type not in ('AIVDM', 'AIVDO'):
        return None

    try:
        total_frags = int(parts[1])
        frag_num = int(parts[2])
    except (ValueError, IndexError):
        return None

    seq_id = parts[3] if len(parts) > 3 else ''
    channel = parts[4] if len(parts) > 4 else ''
    payload = parts[5] if len(parts) > 5 else ''
    fill_bits = 0
    if len(parts) > 6:
        try:
            fill_bits = int(parts[6])
        except ValueError:
            fill_bits = 0

    return {
        'type': sentence_type,
        'total_fragments': total_frags,
        'fragment_num': frag_num,
        'seq_id': seq_id,
        'channel': channel,
        'payload': payload,
        'fill_bits': fill_bits,
    }


def decode_msg_1_2_3(bits):
    if len(bits) < 149:
        return None

    msg_type = _bits_to_uint(bits, 0, 6)
    repeat = _bits_to_uint(bits, 6, 2)
    mmsi = _bits_to_uint(bits, 8, 30)
    nav_status = _bits_to_uint(bits, 38, 4)
    rot_raw = _bits_to_int(bits, 42, 8)
    sog_raw = _bits_to_uint(bits, 46, 10)
    pos_acc = _bits_to_uint(bits, 56, 1)
    lon_raw = _bits_to_int(bits, 57, 28)
    lat_raw = _bits_to_int(bits, 85, 27)
    cog_raw = _bits_to_uint(bits, 112, 12)
    hdg_raw = _bits_to_uint(bits, 124, 9)
    timestamp = _bits_to_uint(bits, 133, 6)
    maneuver = _bits_to_uint(bits, 143, 2)
    raim = _bits_to_uint(bits, 148, 1)

    lon = lon_raw / 600000.0 if lon_raw != 0x6791AC0 else None
    lat = lat_raw / 600000.0 if lat_raw != 0x3412140 else None
    sog = sog_raw / 10.0 if sog_raw != 1023 else None
    cog = cog_raw / 10.0 if cog_raw != 3600 else None
    hdg = hdg_raw if hdg_raw != 511 else None

    if rot_raw == -128:
        rot = None
    elif rot_raw > 0:
        rot = (rot_raw / 4.733) ** 2
    elif rot_raw < 0:
        rot = -((abs(rot_raw) / 4.733) ** 2)
    else:
        rot = 0.0

    return {
        'msg_type': msg_type,
        'repeat': repeat,
        'mmsi': str(mmsi).zfill(9),
        'nav_status': nav_status,
        'nav_status_text': NAV_STATUS_CODES.get(nav_status, 'Unknown'),
        'rot': round(rot, 2) if rot is not None else None,
        'sog': sog,
        'position_accuracy': pos_acc,
        'lon': round(lon, 6) if lon is not None else None,
        'lat': round(lat, 6) if lat is not None else None,
        'cog': round(cog, 1) if cog is not None else None,
        'heading': hdg,
        'timestamp': timestamp,
        'maneuver': maneuver,
        'raim': raim,
    }


def decode_msg_4_11(bits):
    if len(bits) < 168:
        return None

    msg_type = _bits_to_uint(bits, 0, 6)
    mmsi = _bits_to_uint(bits, 8, 30)
    year = _bits_to_uint(bits, 38, 14)
    month = _bits_to_uint(bits, 52, 4)
    day = _bits_to_uint(bits, 56, 5)
    hour = _bits_to_uint(bits, 61, 5)
    minute = _bits_to_uint(bits, 66, 6)
    second = _bits_to_uint(bits, 72, 6)
    pos_acc = _bits_to_uint(bits, 78, 1)
    lon_raw = _bits_to_int(bits, 79, 28)
    lat_raw = _bits_to_int(bits, 107, 27)
    epfd = _bits_to_uint(bits, 134, 4)
    raim = _bits_to_uint(bits, 148, 1)

    lon = lon_raw / 600000.0 if lon_raw != 0x6791AC0 else None
    lat = lat_raw / 600000.0 if lat_raw != 0x3412140 else None

    return {
        'msg_type': msg_type,
        'mmsi': str(mmsi).zfill(9),
        'year': year, 'month': month, 'day': day,
        'hour': hour, 'minute': minute, 'second': second,
        'position_accuracy': pos_acc,
        'lon': round(lon, 6) if lon is not None else None,
        'lat': round(lat, 6) if lat is not None else None,
        'epfd': epfd,
        'epfd_text': EPFD_CODES.get(epfd, 'Unknown'),
        'raim': raim,
    }


def decode_msg_5(bits):
    if len(bits) < 420:
        return None

    msg_type = _bits_to_uint(bits, 0, 6)
    mmsi = _bits_to_uint(bits, 8, 30)
    ais_version = _bits_to_uint(bits, 38, 2)
    imo = _bits_to_uint(bits, 40, 30)
    callsign = _bits_to_string(bits, 70, 42)
    shipname = _bits_to_string(bits, 112, 120)
    ship_type = _bits_to_uint(bits, 232, 8)
    to_bow = _bits_to_uint(bits, 240, 9)
    to_stern = _bits_to_uint(bits, 249, 9)
    to_port = _bits_to_uint(bits, 258, 6)
    to_starboard = _bits_to_uint(bits, 264, 6)
    epfd = _bits_to_uint(bits, 270, 4)
    eta_month = _bits_to_uint(bits, 274, 4)
    eta_day = _bits_to_uint(bits, 278, 5)
    eta_hour = _bits_to_uint(bits, 283, 5)
    eta_minute = _bits_to_uint(bits, 288, 6)
    draught = _bits_to_uint(bits, 294, 8) / 10.0
    destination = _bits_to_string(bits, 302, 120)
    dte = _bits_to_uint(bits, 422, 1) if len(bits) > 422 else 0

    length = to_bow + to_stern
    beam = to_port + to_starboard

    return {
        'msg_type': msg_type,
        'mmsi': str(mmsi).zfill(9),
        'ais_version': ais_version,
        'imo': str(imo) if imo > 0 else '',
        'callsign': callsign,
        'shipname': shipname,
        'ship_type': ship_type,
        'ship_type_text': SHIP_TYPE_CODES.get(ship_type, f'Type {ship_type}'),
        'to_bow': to_bow, 'to_stern': to_stern,
        'to_port': to_port, 'to_starboard': to_starboard,
        'length': length, 'beam': beam,
        'epfd': epfd,
        'epfd_text': EPFD_CODES.get(epfd, 'Unknown'),
        'eta_month': eta_month, 'eta_day': eta_day,
        'eta_hour': eta_hour, 'eta_minute': eta_minute,
        'draught': draught,
        'destination': destination,
        'dte': dte,
    }


def decode_msg_18(bits):
    if len(bits) < 168:
        return None

    msg_type = _bits_to_uint(bits, 0, 6)
    mmsi = _bits_to_uint(bits, 8, 30)
    sog_raw = _bits_to_uint(bits, 46, 10)
    pos_acc = _bits_to_uint(bits, 56, 1)
    lon_raw = _bits_to_int(bits, 57, 28)
    lat_raw = _bits_to_int(bits, 85, 27)
    cog_raw = _bits_to_uint(bits, 112, 12)
    hdg_raw = _bits_to_uint(bits, 124, 9)
    timestamp = _bits_to_uint(bits, 133, 6)

    lon = lon_raw / 600000.0 if lon_raw != 0x6791AC0 else None
    lat = lat_raw / 600000.0 if lat_raw != 0x3412140 else None
    sog = sog_raw / 10.0 if sog_raw != 1023 else None
    cog = cog_raw / 10.0 if cog_raw != 3600 else None
    hdg = hdg_raw if hdg_raw != 511 else None

    return {
        'msg_type': msg_type,
        'mmsi': str(mmsi).zfill(9),
        'sog': sog,
        'position_accuracy': pos_acc,
        'lon': round(lon, 6) if lon is not None else None,
        'lat': round(lat, 6) if lat is not None else None,
        'cog': round(cog, 1) if cog is not None else None,
        'heading': hdg,
        'timestamp': timestamp,
        'nav_status': 15,
        'nav_status_text': 'Class B - no status',
    }


def decode_msg_19(bits):
    if len(bits) < 312:
        return None

    msg_type = _bits_to_uint(bits, 0, 6)
    mmsi = _bits_to_uint(bits, 8, 30)
    sog_raw = _bits_to_uint(bits, 46, 10)
    pos_acc = _bits_to_uint(bits, 56, 1)
    lon_raw = _bits_to_int(bits, 57, 28)
    lat_raw = _bits_to_int(bits, 85, 27)
    cog_raw = _bits_to_uint(bits, 112, 12)
    hdg_raw = _bits_to_uint(bits, 124, 9)
    timestamp = _bits_to_uint(bits, 133, 6)
    shipname = _bits_to_string(bits, 143, 120)
    ship_type = _bits_to_uint(bits, 263, 8)
    to_bow = _bits_to_uint(bits, 271, 9)
    to_stern = _bits_to_uint(bits, 280, 9)
    to_port = _bits_to_uint(bits, 289, 6)
    to_starboard = _bits_to_uint(bits, 295, 6)

    lon = lon_raw / 600000.0 if lon_raw != 0x6791AC0 else None
    lat = lat_raw / 600000.0 if lat_raw != 0x3412140 else None
    sog = sog_raw / 10.0 if sog_raw != 1023 else None
    cog = cog_raw / 10.0 if cog_raw != 3600 else None
    hdg = hdg_raw if hdg_raw != 511 else None

    return {
        'msg_type': msg_type,
        'mmsi': str(mmsi).zfill(9),
        'sog': sog,
        'position_accuracy': pos_acc,
        'lon': round(lon, 6) if lon is not None else None,
        'lat': round(lat, 6) if lat is not None else None,
        'cog': round(cog, 1) if cog is not None else None,
        'heading': hdg,
        'timestamp': timestamp,
        'shipname': shipname,
        'ship_type': ship_type,
        'ship_type_text': SHIP_TYPE_CODES.get(ship_type, f'Type {ship_type}'),
        'length': to_bow + to_stern,
        'beam': to_port + to_starboard,
        'nav_status': 15,
        'nav_status_text': 'Class B - extended',
    }


def decode_msg_21(bits):
    if len(bits) < 272:
        return None

    msg_type = _bits_to_uint(bits, 0, 6)
    mmsi = _bits_to_uint(bits, 8, 30)
    aid_type = _bits_to_uint(bits, 38, 5)
    name = _bits_to_string(bits, 43, 120)
    pos_acc = _bits_to_uint(bits, 163, 1)
    lon_raw = _bits_to_int(bits, 164, 28)
    lat_raw = _bits_to_int(bits, 192, 27)
    to_bow = _bits_to_uint(bits, 219, 9)
    to_stern = _bits_to_uint(bits, 228, 9)
    to_port = _bits_to_uint(bits, 237, 6)
    to_starboard = _bits_to_uint(bits, 243, 6)

    lon = lon_raw / 600000.0 if lon_raw != 0x6791AC0 else None
    lat = lat_raw / 600000.0 if lat_raw != 0x3412140 else None

    aid_types = {
        0: 'Default', 1: 'Reference point', 2: 'RACON', 3: 'Fixed structure',
        4: 'Spare', 5: 'Light without sectors', 6: 'Light with sectors',
        7: 'Leading light front', 8: 'Leading light rear', 9: 'Beacon cardinal N',
        10: 'Beacon cardinal E', 11: 'Beacon cardinal S', 12: 'Beacon cardinal W',
        13: 'Beacon port hand', 14: 'Beacon starboard hand',
        15: 'Beacon preferred channel port', 16: 'Beacon preferred channel starboard',
        17: 'Beacon isolated danger', 18: 'Beacon safe water',
        19: 'Beacon special mark', 20: 'Beacon light vessel/LANBY/FABs',
        21: 'Buoy cardinal N', 22: 'Buoy cardinal E', 23: 'Buoy cardinal S',
        24: 'Buoy cardinal W', 25: 'Buoy port hand', 26: 'Buoy starboard hand',
        27: 'Buoy preferred channel port', 28: 'Buoy preferred channel starboard',
        29: 'Buoy isolated danger', 30: 'Buoy safe water', 31: 'Buoy special mark',
    }

    return {
        'msg_type': msg_type,
        'mmsi': str(mmsi).zfill(9),
        'aid_type': aid_type,
        'aid_type_text': aid_types.get(aid_type, f'Type {aid_type}'),
        'name': name,
        'position_accuracy': pos_acc,
        'lon': round(lon, 6) if lon is not None else None,
        'lat': round(lat, 6) if lat is not None else None,
        'length': to_bow + to_stern,
        'beam': to_port + to_starboard,
    }


def decode_msg_24(bits):
    if len(bits) < 160:
        return None

    msg_type = _bits_to_uint(bits, 0, 6)
    mmsi = _bits_to_uint(bits, 8, 30)
    part = _bits_to_uint(bits, 38, 2)

    result = {
        'msg_type': msg_type,
        'mmsi': str(mmsi).zfill(9),
        'part': part,
    }

    if part == 0:
        shipname = _bits_to_string(bits, 40, 120)
        result['shipname'] = shipname
    elif part == 1 and len(bits) >= 168:
        ship_type = _bits_to_uint(bits, 40, 8)
        vendor_id = _bits_to_string(bits, 48, 18)
        callsign = _bits_to_string(bits, 90, 42)
        to_bow = _bits_to_uint(bits, 132, 9)
        to_stern = _bits_to_uint(bits, 141, 9)
        to_port = _bits_to_uint(bits, 150, 6)
        to_starboard = _bits_to_uint(bits, 156, 6)
        result.update({
            'ship_type': ship_type,
            'ship_type_text': SHIP_TYPE_CODES.get(ship_type, f'Type {ship_type}'),
            'vendor_id': vendor_id,
            'callsign': callsign,
            'length': to_bow + to_stern,
            'beam': to_port + to_starboard,
        })

    return result


def decode_ais_payload(payload, fill_bits=0, msg_type_hint=None):
    bits = _payload_to_bits(payload, fill_bits)
    if len(bits) < 6:
        return None

    msg_type = _bits_to_uint(bits, 0, 6)

    if msg_type in (1, 2, 3):
        return decode_msg_1_2_3(bits)
    elif msg_type in (4, 11):
        return decode_msg_4_11(bits)
    elif msg_type == 5:
        return decode_msg_5(bits)
    elif msg_type == 18:
        return decode_msg_18(bits)
    elif msg_type == 19:
        return decode_msg_19(bits)
    elif msg_type == 21:
        return decode_msg_21(bits)
    elif msg_type == 24:
        return decode_msg_24(bits)
    else:
        mmsi = _bits_to_uint(bits, 8, 30) if len(bits) >= 38 else 0
        return {
            'msg_type': msg_type,
            'mmsi': str(mmsi).zfill(9),
            'raw_bits_length': len(bits),
        }


class AISDecoder:
    def __init__(self):
        self._lock = threading.RLock()
        self.multipart_buffer = {}
        self.vessels = {}
        self.base_stations = {}
        self.aids_to_nav = {}
        self.stats = {
            'total_messages': 0,
            'decoded_ok': 0,
            'decode_errors': 0,
            'msg_type_counts': {},
            'unique_mmsi': set(),
        }

    def process_line(self, line):
        sentence = parse_nmea_sentence(line)
        if not sentence:
            return None

        self.stats['total_messages'] += 1

        if sentence['total_fragments'] == 1:
            try:
                decoded = decode_ais_payload(sentence['payload'], sentence['fill_bits'])
                if decoded:
                    self.stats['decoded_ok'] += 1
                    mt = decoded.get('msg_type', 0)
                    self.stats['msg_type_counts'][mt] = self.stats['msg_type_counts'].get(mt, 0) + 1
                    self._update_vessel(decoded)
                    return decoded
            except Exception as e:
                self.stats['decode_errors'] += 1
                logger.warning(f'AIS decode error (single fragment): {e}')
                return None
        else:
            key = (sentence['seq_id'], sentence['channel'])
            if sentence['fragment_num'] == 1:
                self.multipart_buffer[key] = {
                    'total': sentence['total_fragments'],
                    'parts': {1: sentence['payload']},
                    'fill_bits': sentence['fill_bits'],
                }
            elif key in self.multipart_buffer:
                buf = self.multipart_buffer[key]
                buf['parts'][sentence['fragment_num']] = sentence['payload']
                buf['fill_bits'] = sentence['fill_bits']

                if len(buf['parts']) == buf['total']:
                    combined = ''
                    for i in range(1, buf['total'] + 1):
                        combined += buf['parts'].get(i, '')
                    del self.multipart_buffer[key]

                    try:
                        decoded = decode_ais_payload(combined, buf['fill_bits'])
                        if decoded:
                            self.stats['decoded_ok'] += 1
                            mt = decoded.get('msg_type', 0)
                            self.stats['msg_type_counts'][mt] = self.stats['msg_type_counts'].get(mt, 0) + 1
                            self._update_vessel(decoded)
                            return decoded
                    except Exception as e:
                        self.stats['decode_errors'] += 1
                        return None

        return None

    def _update_vessel(self, decoded):
        mmsi = decoded.get('mmsi', '')
        if not mmsi or mmsi == '000000000':
            return

        self.stats['unique_mmsi'].add(mmsi)
        msg_type = decoded.get('msg_type', 0)

        if msg_type in (1, 2, 3, 18, 19):
            if mmsi not in self.vessels:
                self.vessels[mmsi] = {
                    'mmsi': mmsi,
                    'positions': [],
                    'last_update': datetime.now(timezone.utc).isoformat(),
                }

            v = self.vessels[mmsi]
            v['last_update'] = datetime.now(timezone.utc).isoformat()

            if decoded.get('lat') is not None and decoded.get('lon') is not None:
                lat = decoded['lat']
                lon = decoded['lon']
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    v['lat'] = lat
                    v['lon'] = lon
                    v['positions'].append({
                        'lat': lat, 'lon': lon,
                        'ts': v['last_update'],
                    })
                    if len(v['positions']) > 50:
                        v['positions'] = v['positions'][-50:]

            if decoded.get('sog') is not None:
                v['sog'] = decoded['sog']
            if decoded.get('cog') is not None:
                v['cog'] = decoded['cog']
            if decoded.get('heading') is not None:
                v['heading'] = decoded['heading']
            if decoded.get('nav_status') is not None:
                v['nav_status'] = decoded['nav_status']
                v['nav_status_text'] = decoded.get('nav_status_text', '')
            if decoded.get('rot') is not None:
                v['rot'] = decoded['rot']

            if msg_type == 19:
                if decoded.get('shipname'):
                    v['shipname'] = decoded['shipname']
                if decoded.get('ship_type'):
                    v['ship_type'] = decoded['ship_type']
                    v['ship_type_text'] = decoded.get('ship_type_text', '')
                if decoded.get('length'):
                    v['length'] = decoded['length']
                if decoded.get('beam'):
                    v['beam'] = decoded['beam']

        elif msg_type == 5:
            if mmsi not in self.vessels:
                self.vessels[mmsi] = {
                    'mmsi': mmsi,
                    'positions': [],
                    'last_update': datetime.now(timezone.utc).isoformat(),
                }
            v = self.vessels[mmsi]
            v['imo'] = decoded.get('imo', '')
            v['callsign'] = decoded.get('callsign', '')
            v['shipname'] = decoded.get('shipname', '')
            v['ship_type'] = decoded.get('ship_type', 0)
            v['ship_type_text'] = decoded.get('ship_type_text', '')
            v['length'] = decoded.get('length', 0)
            v['beam'] = decoded.get('beam', 0)
            v['draught'] = decoded.get('draught', 0)
            v['destination'] = decoded.get('destination', '')
            v['eta_month'] = decoded.get('eta_month', 0)
            v['eta_day'] = decoded.get('eta_day', 0)
            v['eta_hour'] = decoded.get('eta_hour', 0)
            v['eta_minute'] = decoded.get('eta_minute', 0)
            v['last_update'] = datetime.now(timezone.utc).isoformat()

        elif msg_type == 24:
            if mmsi not in self.vessels:
                self.vessels[mmsi] = {
                    'mmsi': mmsi,
                    'positions': [],
                    'last_update': datetime.now(timezone.utc).isoformat(),
                }
            v = self.vessels[mmsi]
            if decoded.get('shipname'):
                v['shipname'] = decoded['shipname']
            if decoded.get('callsign'):
                v['callsign'] = decoded['callsign']
            if decoded.get('ship_type'):
                v['ship_type'] = decoded['ship_type']
                v['ship_type_text'] = decoded.get('ship_type_text', '')
            if decoded.get('length'):
                v['length'] = decoded['length']
            if decoded.get('beam'):
                v['beam'] = decoded['beam']
            v['last_update'] = datetime.now(timezone.utc).isoformat()

        elif msg_type in (4, 11):
            self.base_stations[mmsi] = {
                'mmsi': mmsi,
                'lat': decoded.get('lat'),
                'lon': decoded.get('lon'),
                'epfd': decoded.get('epfd_text', ''),
                'last_update': datetime.now(timezone.utc).isoformat(),
            }

        elif msg_type == 21:
            self.aids_to_nav[mmsi] = {
                'mmsi': mmsi,
                'name': decoded.get('name', ''),
                'aid_type': decoded.get('aid_type_text', ''),
                'lat': decoded.get('lat'),
                'lon': decoded.get('lon'),
                'last_update': datetime.now(timezone.utc).isoformat(),
            }

    def process_file(self, filepath, max_lines=None):
        count = 0
        with open(filepath, 'r', errors='replace') as f:
            for line in f:
                self.process_line(line)
                count += 1
                if max_lines and count >= max_lines:
                    break
        return self.get_summary()

    def get_vessels_with_position(self):
        result = []
        for mmsi, v in self.vessels.items():
            if 'lat' in v and 'lon' in v:
                result.append(v)
        return sorted(result, key=lambda x: x.get('last_update', ''), reverse=True)

    def get_summary(self):
        vessels_with_pos = len([v for v in self.vessels.values() if 'lat' in v and 'lon' in v])
        vessels_with_name = len([v for v in self.vessels.values() if v.get('shipname')])

        return {
            'total_messages': self.stats['total_messages'],
            'decoded_ok': self.stats['decoded_ok'],
            'decode_errors': self.stats['decode_errors'],
            'decode_rate': round(self.stats['decoded_ok'] / max(1, self.stats['total_messages']) * 100, 1),
            'msg_type_counts': dict(self.stats['msg_type_counts']),
            'unique_mmsi': len(self.stats['unique_mmsi']),
            'total_vessels': len(self.vessels),
            'vessels_with_position': vessels_with_pos,
            'vessels_with_name': vessels_with_name,
            'base_stations': len(self.base_stations),
            'aids_to_nav': len(self.aids_to_nav),
        }


    def _csv_int(self, fields, header_map, key, default=0):
        if key not in header_map:
            return default
        val = fields[header_map[key]].strip().strip('"')
        if not val:
            return default
        try:
            return int(val)
        except ValueError:
            return default

    def _csv_str(self, fields, header_map, key, default=''):
        if key not in header_map:
            return default
        return fields[header_map[key]].strip().strip('"')

    def process_csv_line(self, fields, header_map):
        try:
            mmsi = str(fields[header_map['MMSI']]).strip().strip('"').zfill(9)
            if not mmsi or mmsi == '000000000':
                return None

            self.stats['total_messages'] += 1
            self.stats['unique_mmsi'].add(mmsi)

            tstamp = self._csv_int(fields, header_map, 'TSTAMP')
            lat_raw = self._csv_int(fields, header_map, 'LATITUDE')
            lon_raw = self._csv_int(fields, header_map, 'LONGITUDE')
            cog_raw = self._csv_int(fields, header_map, 'COG')
            sog_raw = self._csv_int(fields, header_map, 'SOG')
            hdg_raw = self._csv_int(fields, header_map, 'HEADING', 511)
            nav_stat = self._csv_int(fields, header_map, 'NAVSTAT', 15)
            imo_raw = self._csv_str(fields, header_map, 'IMO')
            name = self._csv_str(fields, header_map, 'NAME')
            callsign = self._csv_str(fields, header_map, 'CALLSIGN')
            ship_type = self._csv_int(fields, header_map, 'TYPE')
            a = self._csv_int(fields, header_map, 'A')
            b = self._csv_int(fields, header_map, 'B')
            c = self._csv_int(fields, header_map, 'C')
            d = self._csv_int(fields, header_map, 'D')
            draught_raw = self._csv_int(fields, header_map, 'DRAUGHT')
            dest = self._csv_str(fields, header_map, 'DEST')
            eta_raw = self._csv_int(fields, header_map, 'ETA')

            lat = lat_raw / 600000.0
            lon = lon_raw / 600000.0
            cog = cog_raw / 10.0
            sog = sog_raw / 10.0
            hdg = hdg_raw if hdg_raw != 511 else None
            draught = draught_raw / 10.0
            length = a + b
            beam = c + d

            if lat < -90 or lat > 90 or lon < -180 or lon > 180:
                self.stats['decode_errors'] += 1
                return None
            if lat == 0 and lon == 0:
                self.stats['decode_errors'] += 1
                return None

            ts_str = datetime.fromtimestamp(tstamp, tz=timezone.utc).isoformat() if tstamp > 0 else datetime.now(timezone.utc).isoformat()

            imo_str = str(imo_raw) if imo_raw and str(imo_raw) != '0' else ''

            eta_month = (eta_raw >> 16) & 0xF if eta_raw else 0
            eta_day = (eta_raw >> 11) & 0x1F if eta_raw else 0
            eta_hour = (eta_raw >> 6) & 0x1F if eta_raw else 0
            eta_minute = eta_raw & 0x3F if eta_raw else 0

            if mmsi not in self.vessels:
                self.vessels[mmsi] = {
                    'mmsi': mmsi,
                    'positions': [],
                    'last_update': ts_str,
                }

            v = self.vessels[mmsi]
            v['lat'] = round(lat, 6)
            v['lon'] = round(lon, 6)
            v['sog'] = sog
            v['cog'] = round(cog, 1)
            v['heading'] = hdg
            v['nav_status'] = nav_stat
            v['nav_status_text'] = NAV_STATUS_CODES.get(nav_stat, 'Unknown')
            v['last_update'] = ts_str

            v['positions'].append({'lat': round(lat, 6), 'lon': round(lon, 6), 'ts': ts_str})
            if len(v['positions']) > 50:
                v['positions'] = v['positions'][-50:]

            if name:
                v['shipname'] = name
            if callsign:
                v['callsign'] = callsign
            if imo_str:
                v['imo'] = imo_str
            if ship_type:
                v['ship_type'] = ship_type
                v['ship_type_text'] = SHIP_TYPE_CODES.get(ship_type, f'Type {ship_type}')
            if length:
                v['length'] = length
            if beam:
                v['beam'] = beam
            if draught:
                v['draught'] = draught
            if dest:
                v['destination'] = dest
            if eta_month:
                v['eta_month'] = eta_month
                v['eta_day'] = eta_day
                v['eta_hour'] = eta_hour
                v['eta_minute'] = eta_minute

            self.stats['decoded_ok'] += 1
            mt = 1
            if ship_type >= 1:
                mt = 5
            self.stats['msg_type_counts'][mt] = self.stats['msg_type_counts'].get(mt, 0) + 1

            return v

        except (ValueError, IndexError, KeyError) as e:
            self.stats['decode_errors'] += 1
            return None

    def process_csv_file(self, filepath, max_lines=None):
        import csv
        count = 0
        with open(filepath, 'r', errors='replace') as f:
            first_line = f.readline().strip()
            if not first_line:
                return self.get_summary()

            first_line = first_line.strip('"').replace('""', '"')
            raw_headers = first_line.split(',')
            headers = [h.strip().strip('"').upper() for h in raw_headers]

            header_map = {}
            for i, h in enumerate(headers):
                header_map[h] = i

            required = {'MMSI', 'LATITUDE', 'LONGITUDE'}
            if not required.issubset(set(headers)):
                logger.warning(f"CSV missing required headers: {required - set(headers)}")
                return self.get_summary()

            for line in f:
                line = line.strip()
                if not line:
                    continue

                parts = []
                in_quotes = False
                current = []
                for ch in line:
                    if ch == '"':
                        in_quotes = not in_quotes
                    elif ch == ',' and not in_quotes:
                        parts.append(''.join(current))
                        current = []
                    else:
                        current.append(ch)
                parts.append(''.join(current))

                if len(parts) >= len(required):
                    self.process_csv_line(parts, header_map)

                count += 1
                if max_lines and count >= max_lines:
                    break

        return self.get_summary()

    def process_file(self, filepath, max_lines=None):
        with open(filepath, 'r', errors='replace') as f:
            first_line = f.readline().strip()

        if first_line and ('MMSI' in first_line.upper() and 'LATITUDE' in first_line.upper()):
            return self.process_csv_file(filepath, max_lines)

        count = 0
        with open(filepath, 'r', errors='replace') as f:
            for line in f:
                self.process_line(line)
                count += 1
                if max_lines and count >= max_lines:
                    break
        return self.get_summary()


COUNTRY_NAMES = {
    'ad': 'Andorra', 'ae': 'UAE', 'af': 'Afghanistan', 'ag': 'Antigua & Barbuda',
    'al': 'Albania', 'am': 'Armenia', 'ao': 'Angola', 'ar': 'Argentina',
    'at': 'Austria', 'au': 'Australia', 'az': 'Azerbaijan', 'ba': 'Bosnia',
    'bb': 'Barbados', 'bd': 'Bangladesh', 'be': 'Belgium', 'bg': 'Bulgaria',
    'bh': 'Bahrain', 'bn': 'Brunei', 'bo': 'Bolivia', 'br': 'Brazil',
    'bs': 'Bahamas', 'bw': 'Botswana', 'by': 'Belarus', 'bz': 'Belize',
    'ca': 'Canada', 'cd': 'DR Congo', 'ch': 'Switzerland', 'cl': 'Chile',
    'cm': 'Cameroon', 'cn': 'China', 'co': 'Colombia', 'cr': 'Costa Rica',
    'cu': 'Cuba', 'cv': 'Cape Verde', 'cy': 'Cyprus', 'cz': 'Czechia',
    'de': 'Germany', 'dk': 'Denmark', 'do': 'Dominican Republic', 'dz': 'Algeria',
    'ec': 'Ecuador', 'ee': 'Estonia', 'eg': 'Egypt', 'es': 'Spain',
    'fi': 'Finland', 'fj': 'Fiji', 'fo': 'Faroe Islands', 'fr': 'France',
    'ga': 'Gabon', 'gb': 'United Kingdom', 'ge': 'Georgia', 'gh': 'Ghana',
    'gi': 'Gibraltar', 'gp': 'Guadeloupe', 'gr': 'Greece', 'gt': 'Guatemala',
    'gy': 'Guyana', 'hk': 'Hong Kong', 'hn': 'Honduras', 'hr': 'Croatia',
    'hu': 'Hungary', 'id': 'Indonesia', 'ie': 'Ireland', 'il': 'Israel',
    'in': 'India', 'iq': 'Iraq', 'ir': 'Iran', 'is': 'Iceland',
    'it': 'Italy', 'jm': 'Jamaica', 'jo': 'Jordan', 'jp': 'Japan',
    'ke': 'Kenya', 'kg': 'Kyrgyzstan', 'kh': 'Cambodia', 'kr': 'South Korea',
    'kw': 'Kuwait', 'kz': 'Kazakhstan', 'la': 'Laos', 'lb': 'Lebanon',
    'lk': 'Sri Lanka', 'lt': 'Lithuania', 'lu': 'Luxembourg', 'lv': 'Latvia',
    'ly': 'Libya', 'ma': 'Morocco', 'mc': 'Monaco', 'md': 'Moldova',
    'me': 'Montenegro', 'mf': 'Saint Martin', 'mg': 'Madagascar', 'mk': 'North Macedonia',
    'ml': 'Mali', 'mm': 'Myanmar', 'mn': 'Mongolia', 'mo': 'Macau',
    'mq': 'Martinique', 'mt': 'Malta', 'mu': 'Mauritius', 'mv': 'Maldives',
    'mx': 'Mexico', 'my': 'Malaysia', 'mz': 'Mozambique', 'na': 'Namibia',
    'nc': 'New Caledonia', 'ng': 'Nigeria', 'ni': 'Nicaragua', 'nl': 'Netherlands',
    'no': 'Norway', 'np': 'Nepal', 'nz': 'New Zealand', 'om': 'Oman',
    'pa': 'Panama', 'pe': 'Peru', 'pf': 'French Polynesia', 'pg': 'Papua New Guinea',
    'ph': 'Philippines', 'pk': 'Pakistan', 'pl': 'Poland', 'pr': 'Puerto Rico',
    'pt': 'Portugal', 'py': 'Paraguay', 'qa': 'Qatar', 're': 'Réunion',
    'ro': 'Romania', 'rs': 'Serbia', 'ru': 'Russia', 'rw': 'Rwanda',
    'sa': 'Saudi Arabia', 'se': 'Sweden', 'sg': 'Singapore', 'si': 'Slovenia',
    'sk': 'Slovakia', 'sn': 'Senegal', 'so': 'Somalia', 'sr': 'Suriname',
    'sv': 'El Salvador', 'sy': 'Syria', 'th': 'Thailand', 'tn': 'Tunisia',
    'tr': 'Turkey', 'tt': 'Trinidad & Tobago', 'tw': 'Taiwan', 'tz': 'Tanzania',
    'ua': 'Ukraine', 'ug': 'Uganda', 'us': 'United States', 'uy': 'Uruguay',
    've': 'Venezuela', 'vn': 'Vietnam', 'za': 'South Africa', 'zm': 'Zambia',
    'zw': 'Zimbabwe',
}


class AISStationDirectory:
    def __init__(self):
        self.stations = {}

    def _parse_csv_line(self, line):
        parts = []
        in_quotes = False
        current = []
        for ch in line:
            if ch == '"':
                in_quotes = not in_quotes
            elif ch == ',' and not in_quotes:
                parts.append(''.join(current))
                current = []
            else:
                current.append(ch)
        parts.append(''.join(current))
        return parts

    def _field(self, fields, header_map, key, default=''):
        if key not in header_map:
            return default
        idx = header_map[key]
        if idx >= len(fields):
            return default
        return fields[idx].strip().strip('"')

    def process_stations_csv(self, text):
        lines = text.strip().split('\n')
        if not lines:
            return

        headers = [h.strip().strip('"').upper() for h in lines[0].split(',')]
        header_map = {h: i for i, h in enumerate(headers)}

        if 'ID' not in header_map:
            logger.warning("Stations CSV missing ID header")
            return

        for line in lines[1:]:
            line = line.strip()
            if not line or line.startswith('...'):
                continue

            fields = self._parse_csv_line(line)
            try:
                sid = self._field(fields, header_map, 'ID')
                if not sid:
                    continue

                country_code = self._field(fields, header_map, 'COUNTRY').lower()
                location = self._field(fields, header_map, 'LOCATION')

                lat_str = self._field(fields, header_map, 'LATITUDE', '0')
                lon_str = self._field(fields, header_map, 'LONGITUDE', '0')
                try:
                    lat = float(lat_str) if lat_str else 0.0
                    lon = float(lon_str) if lon_str else 0.0
                except ValueError:
                    lat, lon = 0.0, 0.0

                last_sig = self._field(fields, header_map, 'LAST SIGNAL')
                if not last_sig:
                    last_sig = self._field(fields, header_map, 'LASTUPDATE')

                ships_str = self._field(fields, header_map, 'SHIPS', '0')
                distinct_str = self._field(fields, header_map, 'DISTINCT', '0')
                contributor = self._field(fields, header_map, 'CONTRIBUTOR')

                try:
                    ships = int(ships_str) if ships_str else 0
                except ValueError:
                    ships = 0
                try:
                    distinct = int(distinct_str) if distinct_str else 0
                except ValueError:
                    distinct = 0

                ts_str = ''
                if last_sig:
                    try:
                        ts = int(last_sig)
                        ts_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
                    except (ValueError, OSError):
                        ts_str = last_sig

                has_position = abs(lat) > 0.01 or abs(lon) > 0.01
                is_roaming = 'roaming' in location.lower() if location else False

                station = {
                    'id': sid,
                    'country_code': country_code,
                    'country_name': COUNTRY_NAMES.get(country_code, country_code.upper()),
                    'location': location,
                    'lat': lat,
                    'lon': lon,
                    'has_position': has_position,
                    'is_roaming': is_roaming,
                    'last_signal': ts_str,
                    'last_signal_ts': int(last_sig) if last_sig and last_sig.isdigit() else 0,
                    'ships': ships,
                    'distinct': distinct,
                    'contributor': contributor,
                    'status': 'active' if has_position and not is_roaming else ('roaming' if is_roaming else 'unknown'),
                }

                self.stations[sid] = station

            except (ValueError, IndexError):
                continue

    def process_stations_file(self, filepath):
        with open(filepath, 'r', errors='replace') as f:
            text = f.read()
        self.process_stations_csv(text)
        return self.get_summary()

    def get_stations(self, country=None, active_only=False):
        result = list(self.stations.values())
        if country:
            result = [s for s in result if s.get('country_code', '') == country.lower()]
        if active_only:
            result = [s for s in result if s.get('has_position')]
        return sorted(result, key=lambda s: s.get('last_signal_ts', 0), reverse=True)

    def get_stations_with_position(self):
        return [s for s in self.stations.values() if s.get('has_position') and not s.get('is_roaming')]

    def get_station(self, sid):
        return self.stations.get(str(sid))

    def get_summary(self):
        stations = list(self.stations.values())
        with_pos = [s for s in stations if s.get('has_position')]
        roaming = [s for s in stations if s.get('is_roaming')]
        countries = set(s.get('country_code', '') for s in stations if s.get('country_code'))

        country_counts = {}
        for s in stations:
            cc = s.get('country_code', '??')
            country_counts[cc] = country_counts.get(cc, 0) + 1

        top_countries = sorted(country_counts.items(), key=lambda x: x[1], reverse=True)[:20]

        return {
            'total_stations': len(stations),
            'with_position': len(with_pos),
            'roaming': len(roaming),
            'countries': len(countries),
            'country_list': sorted(countries),
            'top_countries': [{'code': c, 'name': COUNTRY_NAMES.get(c, c.upper()), 'count': n} for c, n in top_countries],
        }


_global_decoder = None
_global_stations = None


def get_decoder():
    global _global_decoder
    if _global_decoder is None:
        _global_decoder = AISDecoder()
    return _global_decoder


def get_station_directory():
    global _global_stations
    if _global_stations is None:
        _global_stations = AISStationDirectory()
    return _global_stations


def process_ais_file(filepath, max_lines=None):
    decoder = get_decoder()
    return decoder.process_file(filepath, max_lines)


def get_live_vessels():
    decoder = get_decoder()
    return decoder.get_vessels_with_position()


def get_ais_stats():
    decoder = get_decoder()
    return decoder.get_summary()


def load_stations_csv(text):
    directory = get_station_directory()
    directory.process_stations_csv(text)
    return directory.get_summary()


def get_stations():
    directory = get_station_directory()
    return directory.get_stations()


def get_stations_summary():
    directory = get_station_directory()
    return directory.get_summary()

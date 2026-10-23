"""International 1X2 market snapshots; independent from lottery handicaps."""
import math
from datetime import datetime, timezone


def analyze_quote(data):
    source = data.get('source', '')
    if not isinstance(source, str) or not source.strip() or len(source) > 200:
        raise ValueError('请填写赔率来源（不超过 200 字）')
    try:
        captured = datetime.fromisoformat(data['captured_at'].replace('Z', '+00:00'))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError('请提供带时区的赔率时间')
    if captured.tzinfo is None or captured > datetime.now(timezone.utc):
        raise ValueError('赔率时间必须带时区且不能在未来')
    values = []
    for key in ('home', 'draw', 'away'):
        try:
            value = float(data[key])
        except (KeyError, ValueError, TypeError):
            raise ValueError('请填写完整的主胜、平局、客胜十进制赔率')
        if isinstance(data[key], bool) or not math.isfinite(value) or not 1 < value <= 1000:
            raise ValueError('十进制赔率必须大于 1 且不超过 1000')
        values.append(value)
    margin = sum(1 / value for value in values)
    if margin < 1 or margin > 1.5:
        raise ValueError('赔率合计异常，请核对是否为同一公司、同一时点的完整胜平负赔率')
    probs = dict(zip(('主胜', '平局', '客胜'), (1 / value / margin for value in values)))
    return {'source': source.strip(), 'captured_at': captured.isoformat(),
            'odds': dict(zip(('home', 'draw', 'away'), values)),
            'probabilities': probs, 'overround': margin - 1,
            'selection': max(probs, key=probs.get)}


def lottery_result(home, away, handicap):
    if isinstance(handicap, bool) or not isinstance(handicap, int):
        raise ValueError('竞彩让球必须是整数；负数表示主队让球')
    difference = home + handicap - away
    return '让胜' if difference > 0 else '让平' if difference == 0 else '让负'

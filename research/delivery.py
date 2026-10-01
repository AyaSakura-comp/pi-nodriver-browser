"""One terminal full-text packet, or explicit incomplete delivery (never clipping)."""
import hashlib
from .evidence import PacketTooLarge, Snapshot


def deliver_progressive(snapshot: Snapshot, packet: str, *, max_bytes=2*1024*1024, max_lines=20000):
    """Validate the frozen artifact and an append-only packet, fail explicitly on caps."""
    raw = snapshot.path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != snapshot.sha256 or len(raw.splitlines()) != snapshot.record_count:
        raise ValueError('frozen evidence changed')
    result = dict(status=snapshot.status, reason=snapshot.reason, gaps=list(snapshot.gaps),
                  artifactPath=str(snapshot.path), sha256=snapshot.sha256,
                  fullEvidenceDelivered=False)
    if len(packet.encode('utf-8')) > max_bytes or len(packet.split('\n')) > max_lines:
        result.update(status='incomplete', reason='packet_too_large',
                      text='Research incomplete: packet_too_large. Full evidence was NOT delivered. '
                           'The immutable artifact is preserved for recovery, not a successful one-turn handoff.')
    else:
        result.update(text=packet, fullEvidenceDelivered=True)
    return result


def deliver(snapshot: Snapshot, *, max_bytes=50*1024, max_lines=2000, compact=False, passages=None):
    result = dict(status=snapshot.status, reason=snapshot.reason, gaps=list(snapshot.gaps),
                  artifactPath=str(snapshot.path), sha256=snapshot.sha256,
                  fullEvidenceDelivered=False)
    try:
        if passages is not None:
            packet = snapshot.render_passages(max_bytes=max_bytes, **passages)
        else:
            packet = (snapshot.render_compact if compact else snapshot.render)(max_bytes=max_bytes)
        # Match Pi's split-newline line accounting, including a final empty line.
        if len(packet.split('\n')) > max_lines:
            raise PacketTooLarge('line limit exceeded')
    except PacketTooLarge:
        result.update(status='incomplete', reason='packet_too_large',
            text='Research incomplete: packet_too_large. Full evidence was NOT delivered. '
                 'The immutable artifact is preserved for recovery, not a successful one-turn handoff.')
    else:
        result.update(text=packet, fullEvidenceDelivered=True)
    return result

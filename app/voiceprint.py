"""Match audio to the speakers a project already has.

When one sentence is re-recognised on its own, its diarization produces fresh,
meaningless cluster numbers. Comparing each new piece against a voiceprint built
from what each existing speaker has already said puts the new sentences back
under the right names instead of inventing "講者 4".
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import sherpa_onnx

from . import config as C
from .config import SAMPLE_RATE

log = logging.getLogger("scribe.voiceprint")

# Cosine similarity below this means "none of the known speakers", which is a
# real answer: the re-run may genuinely have found someone new.
#
# Measured on this pipeline's own output: the same speaker scores ~0.88 (never
# below 0.88 across the test set) while different speakers peak at ~0.33. 0.45
# sat close enough to the noise that a 0.47 match reassigned a sentence to the
# wrong person; 0.60 keeps a wide margin on both sides.
MATCH_THRESHOLD = 0.60

# Too short a clip gives a noisy embedding; better to skip it than to mis-assign.
MIN_CLIP_SEC = 0.6
# Cap how much audio goes into one speaker's profile - more adds little.
MAX_PROFILE_SEC = 30.0

_extractor: sherpa_onnx.SpeakerEmbeddingExtractor | None = None


def _get_extractor(num_threads: int = 2) -> sherpa_onnx.SpeakerEmbeddingExtractor:
    global _extractor
    if _extractor is None:
        from .gpu import resolve as resolve_provider

        # Short clips, same as diarization - the CPU is faster here.
        provider, _ = resolve_provider(C.DIARIZE_PROVIDER)
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(C.SPEAKER_EMBEDDING_MODEL), num_threads=num_threads,
            provider=provider,
        )
        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        log.info("已載入講者聲紋模型（%d 維）", _extractor.dim)
    return _extractor


def embed(samples: np.ndarray) -> np.ndarray | None:
    """Unit-length embedding for one clip, or None if it is too short."""
    if samples.size < int(MIN_CLIP_SEC * SAMPLE_RATE):
        return None
    extractor = _get_extractor()
    stream = extractor.create_stream()
    stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=samples)
    stream.input_finished()
    if not extractor.is_ready(stream):
        return None
    vector = np.asarray(extractor.compute(stream), dtype=np.float32)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else None


def build_profiles(project: dict, samples: np.ndarray,
                   exclude_segment: str | None = None) -> dict[str, np.ndarray]:
    """One averaged voiceprint per speaker, from what they already said.

    `exclude_segment` leaves the sentence being re-recognised out, so it cannot
    vote for its own (possibly wrong) current speaker.
    """
    collected: dict[str, list[np.ndarray]] = {}
    used_sec: dict[str, float] = {}

    # Longest first: the clearest examples of each voice.
    segments = sorted(project.get("segments", []),
                      key=lambda s: s["end"] - s["start"], reverse=True)

    for seg in segments:
        if seg["id"] == exclude_segment:
            continue
        speaker = seg["speaker"]
        if used_sec.get(speaker, 0.0) >= MAX_PROFILE_SEC:
            continue
        clip = samples[int(seg["start"] * SAMPLE_RATE):int(seg["end"] * SAMPLE_RATE)]
        vector = embed(clip)
        if vector is None:
            continue
        collected.setdefault(speaker, []).append(vector)
        used_sec[speaker] = used_sec.get(speaker, 0.0) + (seg["end"] - seg["start"])

    profiles = {}
    for speaker, vectors in collected.items():
        mean = np.mean(vectors, axis=0)
        norm = float(np.linalg.norm(mean))
        if norm > 0:
            profiles[speaker] = mean / norm
    log.info("建立 %d 位講者的聲紋（%s）", len(profiles),
             "、".join(f"{k} {used_sec.get(k, 0):.0f}s" for k in profiles))
    return profiles


def best_match(vector: np.ndarray | None,
               profiles: dict[str, np.ndarray]) -> tuple[str | None, float]:
    """Closest known speaker and the similarity, or (None, score) if too far."""
    if vector is None or not profiles:
        return None, 0.0
    speakers = list(profiles)
    matrix = np.stack([profiles[s] for s in speakers])
    scores = matrix @ vector                      # both sides are unit-length
    index = int(np.argmax(scores))
    score = float(scores[index])
    return (speakers[index] if score >= MATCH_THRESHOLD else None), score


# Two clusters this similar are the same person. Measured on this pipeline:
# same speaker ~0.88 (never below 0.88 across the test set), different speakers
# peak at ~0.33. 0.60 sits with a wide margin either side.
MERGE_THRESHOLD = 0.60

# Audio per cluster used to build its voiceprint for merging.
MERGE_PROFILE_SEC = 12.0

# A cluster needs at least this much speech before its voiceprint is trusted
# enough to represent a real speaker. Everything shorter is assigned to the
# nearest one instead of being allowed to stand alone.
ANCHOR_MIN_SEC = 6.0


def merge_similar_clusters(turns: list[tuple[float, float, int]],
                           samples: np.ndarray,
                           threshold: float = MERGE_THRESHOLD,
                           target: int | None = None
                           ) -> tuple[list[tuple[float, float, int]], int]:
    """Collapse diarization clusters that are actually the same voice.

    Threshold clustering over-splits badly on real recordings - a 25-minute
    meeting with three people came back as 102 speakers, roughly one per
    utterance. Diarization decides using local windows; here every cluster is
    compared as a whole, which is a much easier judgement.

    Returns (turns with renumbered speakers, number of merges performed).
    """
    clusters = sorted({spk for _, _, spk in turns})
    if len(clusters) < 2:
        return turns, 0

    # One voiceprint per cluster, built from its audio *concatenated*.
    #
    # Embedding each turn separately and averaging fails exactly where it is
    # needed most: over-splitting produces clusters made entirely of sub-second
    # fragments, every one of them too short to embed, so they get no profile
    # and survive the merge. Measured on a real 6-minute meeting: 26 clusters
    # went to 19, and every survivor was a fragment. Gluing a cluster's turns
    # together gives even the smallest one something to compare.
    profiles: dict[int, np.ndarray] = {}
    for cluster in clusters:
        pieces, used = [], 0.0
        for start, end in sorted(((s, e) for s, e, k in turns if k == cluster),
                                 key=lambda t: t[1] - t[0], reverse=True):
            if used >= MERGE_PROFILE_SEC:
                break
            clip = samples[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)]
            if clip.size:
                pieces.append(clip)
                used += end - start
        if not pieces:
            continue
        vector = embed(np.concatenate(pieces))
        if vector is not None:
            profiles[cluster] = vector

    if len(profiles) < 2:
        return turns, 0

    # Anchors first, then absorb the rest.
    #
    # An absolute "are these similar enough" test is the wrong question here. A
    # 0.8-second "嗯" produces an embedding that resembles nobody in particular,
    # so it fails every threshold and survives as its own speaker - measured on
    # a real meeting, 26 clusters only came down to 19 that way, and every
    # survivor was a scrap. The answer is not "is it similar enough" but "which
    # of the real speakers is it closest to".
    #
    # So: clusters with enough speech to trust become anchors and are merged
    # among themselves by similarity; every remaining cluster is then assigned
    # to its nearest anchor, threshold or not.
    duration: dict[int, float] = {}
    for start, end, spk in turns:
        duration[spk] = duration.get(spk, 0.0) + (end - start)

    anchors = [c for c in profiles if duration.get(c, 0.0) >= ANCHOR_MIN_SEC]
    if not anchors:                       # nothing substantial - keep the longest
        anchors = [max(profiles, key=lambda c: duration.get(c, 0.0))]

    groups = {c: [c] for c in anchors}
    vectors = {c: profiles[c] for c in anchors}
    merges = 0

    while len(groups) > 1:
        keys = list(groups)
        matrix = np.stack([vectors[k] for k in keys])
        sims = matrix @ matrix.T
        np.fill_diagonal(sims, -1.0)
        i, j = np.unravel_index(int(np.argmax(sims)), sims.shape)
        best = float(sims[i, j])

        if target is not None:
            if len(groups) <= target:
                break
        elif best < threshold:
            break

        a, b = keys[i], keys[j]
        groups[a].extend(groups.pop(b))
        combined = vectors[a] + vectors.pop(b)
        norm = float(np.linalg.norm(combined))
        vectors[a] = combined / norm if norm > 0 else vectors[a]
        merges += 1

    # Every non-anchor cluster joins whichever anchor it is closest to.
    keys = list(groups)
    matrix = np.stack([vectors[k] for k in keys])
    absorbed = 0
    for cluster in profiles:
        if any(cluster in members for members in groups.values()):
            continue
        nearest = keys[int(np.argmax(matrix @ profiles[cluster]))]
        groups[nearest].append(cluster)
        absorbed += 1

    # Clusters too short to embed at all follow the turn before them in time.
    unprofiled = [c for c in clusters if c not in profiles]
    if unprofiled:
        placed = {m: root for root, members in groups.items() for m in members}
        previous = None
        for _, _, spk in turns:
            if spk in placed:
                previous = placed[spk]
            elif previous is not None and spk not in placed:
                groups[previous].append(spk)
                placed[spk] = previous
                absorbed += 1
        for spk in unprofiled:            # anything still loose joins the biggest
            if spk not in placed:
                biggest = max(groups, key=lambda k: sum(duration.get(m, 0.0)
                                                        for m in groups[k]))
                groups[biggest].append(spk)
                absorbed += 1

    if not merges and not absorbed:
        return turns, 0

    # Renumber so speakers come out 0..n-1 in order of first appearance.
    canonical = {member: root for root, members in groups.items() for member in members}
    order: dict[int, int] = {}
    remapped = []
    for start, end, spk in turns:
        root = canonical.get(spk, spk)
        if root not in order:
            order[root] = len(order)
        remapped.append((start, end, order[root]))

    log.info("聲紋合併：%d 群 -> %d 位講者（錨點合併 %d 次，吸收 %d 個小群）",
             len(clusters), len(order), merges, absorbed)
    return remapped, merges + absorbed


def assign(pieces: list[tuple[float, float, int]], samples: np.ndarray,
           profiles: dict[str, np.ndarray]) -> list[dict[str, Any]]:
    """Label each re-recognised piece with an existing speaker where possible.

    Pieces sharing a diarization cluster are decided together - one clip may be
    too short to judge, but the cluster as a whole usually is not.
    """
    by_cluster: dict[int, list[int]] = {}
    for i, (_, _, cluster) in enumerate(pieces):
        by_cluster.setdefault(cluster, []).append(i)

    out: list[dict[str, Any]] = [{} for _ in pieces]
    taken: set[str] = set()

    ranked = []
    for cluster, indices in by_cluster.items():
        vectors = []
        for i in indices:
            start, end, _ = pieces[i]
            vec = embed(samples[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)])
            if vec is not None:
                vectors.append(vec)
        if vectors:
            mean = np.mean(vectors, axis=0)
            norm = float(np.linalg.norm(mean))
            mean = mean / norm if norm > 0 else None
        else:
            mean = None
        speaker, score = best_match(mean, profiles)
        ranked.append((score, cluster, indices, speaker))

    # Confident clusters claim their speaker first, so two clusters do not both
    # collapse onto the same person.
    for score, cluster, indices, speaker in sorted(ranked, reverse=True,
                                                   key=lambda r: r[0]):
        if speaker and speaker in taken:
            speaker = None
        if speaker:
            taken.add(speaker)
        for i in indices:
            out[i] = {"speaker": speaker, "score": round(score, 3), "cluster": cluster}
    return out

package com.example.demo.fault;

import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicReference;
import org.springframework.stereotype.Component;

/**
 * Demo-only fault switches. This bean is intentionally in-memory and per-Pod:
 * toggling it affects only the Pod you port-forwarded to, which is exactly
 * what makes the demo deterministic (no Service load-balancing surprises).
 *
 * <p>WARNING: never ship this controller/state to production. It exists only
 * so a presenter can turn a fault on, show the evidence, and turn it off.
 */
@Component
public class FaultState {

  public enum Mode {
    /** Baseline: one normal downstream call. */
    normal,
    /** The service itself sleeps 300 ms before calling downstream. */
    self_slow,
    /** One downstream call, but the downstream itself is slow (300 ms). */
    downstream_slow,
    /** N+1: 50 separate downstream calls, ~10-20 ms each. Total looks slow,
     *  but no single call is slow -- the count is the evidence. */
    nplus1
  }

  private final AtomicReference<Mode> mode = new AtomicReference<>(Mode.normal);

  /** Unbounded on purpose, for the memory demo. Never evicted except by reset. */
  private final ConcurrentHashMap<String, byte[]> leakCache = new ConcurrentHashMap<>();

  public Mode mode() {
    return mode.get();
  }

  public void mode(Mode mode) {
    this.mode.set(mode);
  }

  /** Allocate {@code megabytes} of retained heap, in 1 MB chunks. */
  public int allocateLeak(int megabytes) {
    int capped = Math.max(1, Math.min(megabytes, 200));
    for (int i = 0; i < capped; i++) {
      leakCache.put(UUID.randomUUID().toString(), new byte[1024 * 1024]);
    }
    return leakCache.size();
  }

  public int leakEntries() {
    return leakCache.size();
  }

  /** Approximate retained leak size in MB (1 entry == 1 MB by construction). */
  public int leakMegabytes() {
    return leakCache.size();
  }

  public void reset() {
    mode.set(Mode.normal);
    leakCache.clear();
  }
}

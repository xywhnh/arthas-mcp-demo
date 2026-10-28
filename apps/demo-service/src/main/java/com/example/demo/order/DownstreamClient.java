package com.example.demo.order;

import org.springframework.stereotype.Component;

/**
 * Simulated downstream (DB / RPC / cache stand-in).
 *
 * <p>Method names are deliberately distinct so that an Arthas {@code trace}
 * reads like a story: {@code getItem} vs {@code getItemSlow} vs
 * {@code loadItemsOneByOne} in OrderService.
 */
@Component
public class DownstreamClient {

  /** Normal downstream call: ~20 ms. */
  public String getItem(String itemId) {
    sleep(20);
    return "item-" + itemId;
  }

  /** Slow downstream call: ~300 ms. Used by the "downstream-slow" fault mode. */
  public String getItemSlow(String itemId) {
    sleep(300);
    return "item-" + itemId;
  }

  private static void sleep(long ms) {
    try {
      Thread.sleep(ms);
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      throw new IllegalStateException("interrupted", e);
    }
  }
}

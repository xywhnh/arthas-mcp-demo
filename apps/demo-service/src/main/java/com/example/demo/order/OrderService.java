package com.example.demo.order;

import com.example.demo.fault.FaultState;
import java.util.ArrayList;
import java.util.List;
import org.springframework.stereotype.Service;

@Service
public class OrderService {

  private final DownstreamClient downstream;
  private final FaultState faults;

  public OrderService(DownstreamClient downstream, FaultState faults) {
    this.downstream = downstream;
    this.faults = faults;
  }

  public Order getOrder(String id) {
    return switch (faults.mode()) {
      case self_slow -> getOrderSelfSlow(id);
      case downstream_slow -> getOrderDownstreamSlow(id);
      case nplus1 -> getOrderNplus1(id);
      case normal -> getOrderNormal(id);
    };
  }

  private Order getOrderNormal(String id) {
    List<String> items = List.of(downstream.getItem("1"));
    return new Order(id, "demo-customer", items, "normal");
  }

  private Order getOrderSelfSlow(String id) {
    // The delay lives HERE, in this service -- trace should show it as
    // self-time of getOrder, not as downstream time.
    sleep(300);
    List<String> items = List.of(downstream.getItem("1"));
    return new Order(id, "demo-customer", items, "self_slow");
  }

  private Order getOrderDownstreamSlow(String id) {
    List<String> items = List.of(downstream.getItemSlow("1"));
    return new Order(id, "demo-customer", items, "downstream_slow");
  }

  private Order getOrderNplus1(String id) {
    // The N+1 pattern: many cheap calls instead of one batch call.
    // Arthas trace/monitor should show ~50 downstream invocations.
    List<String> items = loadItemsOneByOne(50);
    return new Order(id, "demo-customer", items, "nplus1");
  }

  private List<String> loadItemsOneByOne(int count) {
    List<String> items = new ArrayList<>(count);
    for (int i = 0; i < count; i++) {
      items.add(downstream.getItem(String.valueOf(i)));
    }
    return items;
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

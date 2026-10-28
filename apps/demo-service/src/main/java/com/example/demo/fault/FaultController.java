package com.example.demo.fault;

import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * Demo-only fault controls. Reach them via a Pod port-forward, not via the
 * public Service, so the toggle hits exactly one Pod.
 */
@RestController
@RequestMapping("/demo/faults")
public class FaultController {

  private final FaultState faults;

  public FaultController(FaultState faults) {
    this.faults = faults;
  }

  @GetMapping
  public Map<String, Object> status() {
    return Map.of(
        "mode", faults.mode().name(),
        "leakEntries", faults.leakEntries(),
        "leakMegabytesApprox", faults.leakMegabytes());
  }

  @PostMapping("/mode")
  public Map<String, Object> setMode(@RequestParam FaultState.Mode mode) {
    faults.mode(mode);
    return status();
  }

  @PostMapping("/leak")
  public Map<String, Object> leak(@RequestParam(defaultValue = "50") int megabytes) {
    int entries = faults.allocateLeak(megabytes);
    return Map.of("mode", faults.mode().name(), "leakEntries", entries,
        "leakMegabytesApprox", faults.leakMegabytes());
  }

  @PostMapping("/reset")
  public Map<String, Object> reset() {
    faults.reset();
    return status();
  }
}

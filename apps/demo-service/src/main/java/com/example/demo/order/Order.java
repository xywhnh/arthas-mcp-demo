package com.example.demo.order;

import java.util.List;

/** Plain demo DTO. No database is involved: the "downstream" is simulated on purpose. */
public record Order(String id, String customer, List<String> items, String mode) {}

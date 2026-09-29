# Web Application Architecture Patterns

## Monolithic vs Microservices Decision Framework

| Factor | Monolithic | Microservices |
|--------|-----------|---------------|
| Team Size | < 10 developers | > 10 developers |
| Time to Market | Faster initial | Slower initial |
| Deployment | Single unit | Independent services |
| Scaling | Scale whole app | Scale individual services |
| Complexity | Lower cognitive | Higher coordination |

## Common Layered Architecture

```
┌─────────────────┐
│  Presentation   │  ← React/Vue, CLI, API clients
├─────────────────┤
│  Application    │  ← Use cases, orchestration
├─────────────────┤
│  Domain         │  ← Business logic, entities
├─────────────────┤
│  Infrastructure │  ← DB, cache, external APIs
└─────────────────┘
```

## Database Selection Guide

| Scenario | Recommendation |
|----------|---------------|
| Relational data, ACID needed | PostgreSQL |
| Document-oriented, flexible schema | MongoDB |
| Key-value, caching | Redis |
| Full-text search | Elasticsearch |
| Time-series data | InfluxDB/TimescaleDB |

## API Style Decision Matrix

| Style | Best For | Avoid When |
|-------|----------|-----------|
| REST | General purpose, caching | Complex relationships |
| GraphQL | Mobile, aggregating data | Simple CRUD, caching critical |
| gRPC | Internal service communication | Public APIs, browser clients |
| WebSocket | Real-time, bidirectional | One-shot requests |

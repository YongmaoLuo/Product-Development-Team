# Interview Question Templates by Domain

## Web Application

1. **User & Auth**: Who are the users? Authentication method (JWT/session/OAuth)?
2. **Data Model**: What are the core entities? Relationships?
3. **UI Complexity**: Single-page or multi-page? Real-time updates needed?
4. **Integration**: External APIs? Webhooks? File uploads?
5. **Performance**: Expected concurrent users? Response time targets?

## API / Backend Service

1. **Protocol**: REST / GraphQL / gRPC / WebSocket?
2. **Consumers**: Internal services, mobile apps, third parties?
3. **Scale**: Requests per second? Data volume?
4. **Reliability**: SLA requirements? Retry strategies?
5. **Observability**: Logging, metrics, tracing requirements?

## CLI Tool

1. **Target Users**: Developers, DevOps, end users?
2. **Environment**: OS support? Installation method?
3. **Configuration**: Config files, env vars, CLI flags?
4. **Output**: Human-readable, JSON, machine-parseable?
5. **Integration**: Pipe-friendly? Exit codes meaningful?

## Data Processing / ML Pipeline

1. **Input**: Data source (DB, file, stream)? Format? Volume?
2. **Output**: Destination? Format? Latency requirements?
3. **Processing**: Batch or streaming? Frequency?
4. **Error Handling**: Fail-fast or best-effort? Dead letter queue?
5. **Monitoring**: Success/failure alerts? Data quality checks?

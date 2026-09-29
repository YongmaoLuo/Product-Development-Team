# Test Patterns by Type

## Unit Testing

### AAA Pattern (Arrange-Act-Assert)
```python
def test_user_login():
    # Arrange
    user = User(username="test", password="hash")
    
    # Act
    result = user.authenticate("hash")
    
    # Assert
    assert result is True
```

### Test Pyramid
- **70%** Unit tests — fast, isolated, cheap
- **20%** Integration tests — module interactions
- **10%** E2E tests — full user flows, expensive

## Integration Testing

### Test Data Strategies
1. **Factory Pattern** — Generate test data programmatically
2. **Database Snapshots** — Restore known state before tests
3. **Transaction Rollback** — Roll back after each test

### API Testing Checklist
- [ ] Happy path (valid input)
- [ ] Invalid input validation
- [ ] Missing required fields
- [ ] Authentication/authorization
- [ ] Rate limiting
- [ ] Error response format
- [ ] Content negotiation

## E2E Testing

### Critical User Flows
Identify the top 3-5 user journeys that must never break:
1. User registration → login → core action
2. Core CRUD operations
3. Payment/checkout flow (if applicable)
4. Search/filter functionality

### Page Object Model
Abstract UI interactions into reusable page objects:
```python
class LoginPage:
    def login(self, username, password):
        self.fill_username(username)
        self.fill_password(password)
        self.click_submit()
```

## Performance Testing

### Key Metrics
| Metric | Description | Target |
|--------|-------------|--------|
| Response Time | P50/P95/P99 latency | P95 < 200ms |
| Throughput | Requests per second | > 1000 RPS |
| Error Rate | Failed requests / total | < 0.1% |
| Resource Usage | CPU/Memory under load | < 80% |

### Load Test Scenarios
1. **Baseline** — Normal expected load
2. **Peak** — 2x expected load
3. **Stress** — Until failure to find breaking point
4. **Spike** — Sudden traffic increase
5. **Soak** — Sustained load over hours

## Security Testing

### OWASP Top 10 Coverage
- [ ] Injection (SQL, NoSQL, Command)
- [ ] Broken Authentication
- [ ] Sensitive Data Exposure
- [ ] Broken Access Control
- [ ] Security Misconfiguration

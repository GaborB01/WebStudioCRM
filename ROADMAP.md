# Development Roadmap

## Completed
- [x] Client master data
- [x] Client contact persons management
- [x] Project module
- [x] Task module

### Phase 4: Quoting & Document Management
- [ ] **Dedicated Quoting Module**: Detailed line-item quotes (quantity, rate, total) tied to Projects.
- [ ] **Contract & Document Storage**: `Contract` model for PDFs (NDAs, Development Agreements, SLAs) with signed dates and status tracking.

### Phase 5: Communication Sync & Monitoring
- [ ] **Automatic Email & Activity Logging**: Auto-matching incoming/outgoing emails to Clients and Projects via contact email addresses or reference IDs (e.g., `[PRJ-101]`).
- [ ] **Deployment & Monitoring Data**: Tracking live/staging URLs, hosting providers, repository links, SSL expiry dates, and uptime monitors tied to maintenance contracts.

### Phase 6: API Layer & React Dashboard
- [ ] **Django REST API**: Building endpoints for all CRM entities (Clients, Projects, Tasks, Contracts etc..).
- [ ] **React Frontend Dashboard**: Modern, custom web interface replacing/complementing the Django Admin for daily studio management, pipeline Kanban boards, and analytics.
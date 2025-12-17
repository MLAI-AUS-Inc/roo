# Secure Points System API Implementation Guide

We need to implement a secure API endpoint in the `mlai-backend` Django project that allows the Roo agent (via Slackbot) to award and deduct points. Roo will **never** write to the database directly; it will strictly use this API.

## Core Requirement: Secure Award/Deduct Endpoint

Create or verify the existence of the following API endpoint:

- **URL**: `POST /api/v1/points/admin/award/`
- **Authentication**: Custom `X-API-Key` header matching `INTERNAL_API_KEY` env var.
- **Purpose**: Process manual point adjustments (awards and deductions).

### Request Specification

Roo will send a POST request with the following structure:

**Headers:**
```
Content-Type: application/json
X-API-Key: <your-secure-internal-api-key>
```
*Note: This key must be validated against `INTERNAL_API_KEY` in settings. It is separate from user session auth.*

**Body (JSON):**
```json
{
  "admin_slack_id": "U12345",    // Slack ID of the user REQUESTING the change (needs admin check)
  "target_slack_id": "U67890",   // Slack ID of the user RECEIVING points
  "points": 10,                  // Integer amount (positive to award, negative to deduct)
  "reason": "Helping with event" // Short description for the ledger
}
```

### Backend Logic Requirements

The backend handler for this endpoint must perform these 4 checks/actions in order:

1.  **Security Check (API Key)**:
    - Validate that the `X-API-Key` header matches `settings.INTERNAL_API_KEY`.
    - fail with `401 Unauthorized` if invalid.

2.  **Authorization Check (Business Logic)**:
    - Look up the user associated with `admin_slack_id`.
    - Check if this user has permission to award points (e.g., call `service.is_points_admin(user)`).
    - **Crucial**: Even with a valid API key, if the *requesting user* is not an admin, fail with `403 Forbidden`. Roo is just the messenger; the human commander must have rights.

3.  **Validation**:
    - Ensure `target_slack_id` corresponds to a valid user account (or create a stub if your system allows).
    - Ensure `points` is a valid integer (allow negative for deductions).
    - Ensure `reason` is provided.

4.  **Transaction Execution**:
    - Create a ledger entry recording the transaction.
    - Update the target user's balance atomically.
    - Log the action for audit purposes.

### Response Specification

**Success (200 OK):**
```json
{
  "success": true,
  "new_balance": 150,
  "ledger_id": 42,
  "message": "Awarded 10 points to @user"
}
```

**Error Responses:**
- `401 Unauthorized`: Invalid API Key.
- `403 Forbidden`: `admin_slack_id` is not a Points Admin.
- `400 Bad Request`: Missing fields or invalid user ID.

## Summary for Implementation

Please ensure the `mlai-backend` project has:
1.  `INTERNAL_API_KEY` added to `.env` and `settings.py`.
2.  A Django view/APIView for `/api/v1/points/admin/award/` that implements the logic above.
3.  Logic to resolve Slack IDs to internal User objects.
4.  Service layer handles for `award_points(from_user, to_user, amount, reason)`.

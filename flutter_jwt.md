# Flutter JWT Authentication Contract

The backend accepts one HTTP endpoint only:

```text
POST https://fitness.moveneticsdigital.com/
```

The request `action` selects the operation.

## 1. Authentication flow

```text
Flutter Google Login
  -> Firebase Auth user
  -> Firebase ID token
  -> POST / with action auth.firebase_exchange
  -> backend verifies Firebase token
  -> backend creates/loads app_user
  -> backend creates auth_session
  -> backend returns access_token + refresh_token
```

The Firebase ID token is used only for `auth.firebase_exchange`. Do not send it to normal actions.

## 2. Firebase exchange

No backend access JWT is required.

```http
POST /
Content-Type: application/json
```

```json
{
  "action": "auth.firebase_exchange",
  "firebase_id_token": "<firebase-id-token>",
  "device_id": "device-123",
  "device_name": "Abdul Android",
  "platform": "android",
  "app_version": "1.0.0"
}
```

Only these fields are accepted:

| Field | Required | Source |
|---|---:|---|
| `action` | yes | Flutter constant |
| `firebase_id_token` | yes | Firebase Auth |
| `device_id` | no | Flutter/device identifier |
| `device_name` | no | Flutter |
| `platform` | no | Flutter, for example `android` or `ios` |
| `app_version` | no | Flutter |

Response:

```json
{
  "success": true,
  "request_id": "...",
  "action": "auth.firebase_exchange",
  "access_token": "<backend-jwt>",
  "refresh_token": "<opaque-refresh-token>",
  "token_type": "Bearer",
  "expires_in": 900,
  "refresh_expires_in": 2592000,
  "user": {
    "user_id": "<local-user-id>",
    "firebase_uid": "<firebase-uid>",
    "email": "user@example.com"
  }
}
```

Store the backend tokens in platform secure storage. Do not store them in ordinary preferences or logs.

## 3. What the server stores

Flutter supplies only optional device metadata. The server generates and stores the security fields:

| Session field | Created by |
|---|---|
| `session_id` | server UUID |
| `user_id` | server user mapping |
| `refresh_hash` | server SHA-256 hash of opaque token |
| `device_id` | Flutter, optional |
| `device_name` | Flutter, optional |
| `platform` | Flutter, optional |
| `app_version` | Flutter, optional |
| `created_at` | database |
| `last_used_at` | database/server |
| `expires_at` | server configuration |
| `revoked_at` | server on logout/rotation |
| `replaced_by` | server during rotation |

Flutter must never send `session_id`, `refresh_hash`, `expires_at`, `revoked_at`, or `replaced_by`.

## 4. Normal API actions

Use the backend access token:

```http
Authorization: Bearer <access-token>
Content-Type: application/json
```

Example chat request:

```json
{
  "action": "chat",
  "message": "How was my sleep this week?",
  "session_id": "conversation-123"
}
```

The `session_id` here is a conversation ID, not an authentication session ID.

## 5. Refresh

No backend access JWT is required. Send the current refresh token:

```json
{
  "action": "auth.refresh",
  "refresh_token": "<current-refresh-token>",
  "device_id": "device-123"
}
```

The server rotates the refresh token. Replace both stored tokens with the response values.

```json
{
  "success": true,
  "action": "auth.refresh",
  "access_token": "<new-backend-jwt>",
  "refresh_token": "<new-refresh-token>",
  "expires_in": 900,
  "refresh_expires_in": 2592000
}
```

If refresh returns `401`, clear backend tokens and perform Firebase login/exchange again.

## 6. Logout current device

Requires the backend access JWT:

```http
Authorization: Bearer <access-token>
```

```json
{
  "action": "auth.logout",
  "refresh_token": "<current-refresh-token>"
}
```

Then clear local backend tokens and call Firebase sign-out.

## 7. Logout all devices

Requires the backend access JWT:

```http
Authorization: Bearer <access-token>
```

```json
{
  "action": "auth.logout_all"
}
```

## 8. Recommended Flutter request policy

```text
1. Login with Firebase.
2. Exchange the Firebase ID token once.
3. Add backend access JWT to normal requests.
4. On one 401, refresh once using refresh_token.
5. Replace both tokens because refresh rotation is enabled.
6. Retry the original request once.
7. If refresh fails, clear tokens and require Firebase exchange again.
```

Do not run multiple refresh requests concurrently. Use one shared refresh operation.

## 9. Security rules

- Never sign backend JWTs in Flutter.
- Never include a user ID to select whose data is accessed.
- Never send the Firebase Admin service-account JSON to Flutter.
- Never send refresh tokens in URLs.
- Never log Firebase, access, or refresh tokens.
- Use HTTPS in production.
- Keep the conversation `session_id` separate from `auth_session.session_id`.

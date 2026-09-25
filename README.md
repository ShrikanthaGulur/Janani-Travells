# Janani

Local web desk for fleet operations, plus a phone-sized driver view and a small JSON API an Android client can call.

## Run

```bash
cd ~/Documents/Transport_Managment_System_app
.venv/bin/python app.py
```

Open http://127.0.0.1:5050

## Sign in

SMS is not connected. After you request an OTP, the development code is shown on the login page.

| Role | Phone |
| --- | --- |
| Admin | 9000000001 |
| Manager | 9000000002 |
| Driver Ravi | 9000000011 |
| Driver Anita | 9000000012 |

Managers can use every module except Audit. Drivers see their own profile, the assigned vehicle, its maintenance, and can enter fuel and KM readings.

## Android

The driver pages are built for a phone screen. Add the site to the Android home screen from the browser. The same session cookies work with:

- `POST /api/auth/otp` `{ "phone": "9000000011" }`
- `POST /api/auth/verify` `{ "phone", "code" }`
- `GET /api/me`
- `POST /api/fuel` `{ "vehicle_id", "litres", "cost", "km_reading", "entry_date" }`

GPS, FASTag, route optimization, and live push/SMS providers are left for a later release.

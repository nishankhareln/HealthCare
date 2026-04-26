# Flutter Latency Optimization Guide — Samni Labs

These are 10 things to implement in the Flutter app to make it feel fast and real-time.

---

## 1. Live Transcription (saves 25-30 seconds per patient)

Do NOT record audio and upload later. Stream audio LIVE to Amazon Transcribe Medical while the caregiver is speaking. Text appears on screen word by word as they talk. When they tap "Stop," the transcript is already complete. Zero waiting.

How to do it:
- Call our backend `POST /get-transcribe-token` to get temporary AWS credentials
- Open a WebSocket to `wss://transcribestreaming.us-east-1.amazonaws.com:8443`
- Parameters: language `en-US`, specialty `PRIMARYCARE`, sample rate `16000`, encoding `pcm`
- Stream audio chunks (100ms each) from the microphone through the WebSocket
- Transcribe Medical sends back partial transcript results through the same WebSocket
- Display the text on screen as it arrives
- When caregiver taps "Done," close the WebSocket
- Save the final transcript text to S3 (tiny text file, instant upload)
- Save the audio file to S3 as backup (in background, don't wait for it)

Packages needed: `web_socket_channel`, `aws_signature_v4`, `flutter_sound` or `record`

---

## 2. Pre-fetch Presigned URLs (saves 1-2 seconds per upload)

Do NOT wait until the caregiver taps "Upload" to request the presigned URL. Fetch it in advance.

The moment they open the "Add Patient" screen, call `POST /get-upload-url` in the background. By the time they select the PDF file, the upload URL is already ready. They never wait for the URL generation.

Same for audio backup — fetch the audio upload URL before recording starts.

---

## 3. Use AAC/M4A Not WAV (saves 1-3 seconds per upload)

```
Format: AAC or M4A (NOT WAV)
Sample rate: 16000 Hz
Channels: 1 (mono)
noiseSuppress: true
```

WAV files are 5-10MB for a 4-minute recording. AAC/M4A is 0.5-1MB for the same duration. Smaller file = faster upload. Transcribe Medical accepts both formats. There is no reason to use WAV.

---

## 4. Upload Audio Backup in Background (saves 2-3 seconds)

After the caregiver taps "Done," two things need to happen: save the transcript text to S3 and save the audio file to S3 as backup.

Do both in PARALLEL, not one after another.

The transcript is tiny (few KB) — it uploads instantly. Call `POST /reassessment` as soon as the transcript is uploaded. Do NOT wait for the audio backup upload to finish. Let the audio upload continue in the background while the backend is already processing the transcript.

```dart
// Do this
await Future.wait([
  uploadTranscript(),  // tiny, instant
  uploadAudioBackup(), // larger, takes 2-3 sec
]);
callReassessmentEndpoint(); // call as soon as transcript is done

// NOT this
await uploadTranscript();
await uploadAudioBackup();  // caregiver waits here for no reason
callReassessmentEndpoint();
```

Actually even better — don't await the audio upload at all before calling reassessment:

```dart
uploadAudioBackup();  // fire and forget in background
await uploadTranscript();
callReassessmentEndpoint();  // don't wait for audio backup
```

---

## 5. Parallel API Calls on App Launch (saves 10-15 seconds)

When the app opens and loads the patient list, call status for all patients at the same time.

```dart
// GOOD — all 16 patients load in 1 second
final futures = patients.map((p) => getPatientStatus(p.id));
final results = await Future.wait(futures);

// BAD — takes 16 seconds (1 second per patient, sequential)
for (final p in patients) {
  await getPatientStatus(p.id);  // waits for each one before starting next
}
```

---

## 6. Smart Polling Intervals (feels 2-3x faster)

After submitting a reassessment, poll `GET /patient/{id}/status` to check progress. Use decreasing frequency:

```dart
// First 30 seconds: poll every 3 seconds (processing is likely happening now)
// After 30 seconds: poll every 5 seconds (might be waiting for something)
// After 2 minutes: poll every 10 seconds (something is taking long)
// After 5 minutes: stop polling, show "Taking longer than expected. We'll notify you when ready."
```

Never poll every 1 second — it's wasteful and drains battery. Never poll every 10 seconds from the start — it feels unresponsive.

---

## 7. Cache Patient Data Locally (instant screen switches)

After the app loads a patient's data from the backend, keep it in memory (NOT on disk — HIPAA requires no PHI cached to storage).

If the caregiver switches between patients, show the cached data instantly instead of calling the API again. Only refresh from the API when:
- They pull-to-refresh manually
- They return from a reassessment or review
- The cached data is older than 5 minutes
- A push notification arrives for that patient

```dart
Map<String, PatientData> _cache = {};
DateTime? _lastFetched;

Future<PatientData> getPatient(String id) {
  if (_cache.containsKey(id) && _lastFetched!.isAfter(DateTime.now().subtract(Duration(minutes: 5)))) {
    return _cache[id]!;  // instant, no API call
  }
  // fetch from API, update cache
}
```

---

## 8. Optimistic UI Updates (no spinner waiting)

When the caregiver submits review decisions (approve/reject), do NOT show a loading spinner and wait for the backend response.

Immediately update the screen: show "Submitted! Processing your changes..." and navigate them back to the patient list. The backend processes in the background. When the push notification arrives saying "PDF ready," update the patient's status.

Same for reassessment submission — after calling `POST /reassessment`, immediately show "Processing your update. We'll notify you when it's ready." Don't make them stare at a spinner.

```dart
// GOOD
submitReviewDecisions();  // fire the API call
showSuccessMessage("Submitted! We'll notify you when ready.");
navigateToPatientList();  // move on immediately

// BAD
showSpinner();
await submitReviewDecisions();  // caregiver stares at spinner
hideSpinner();
showSuccessMessage("Done!");
```

---

## 9. Push Notification Deep Links (saves 3-5 seconds per notification)

When a push notification arrives and the caregiver taps it, open the EXACT right screen. Not the home screen. Not the patient list.

Three notification types from our backend:

- `notification_type: "review_needed"` → open the Review screen for that patient with flagged items already loaded
- `notification_type: "pipeline_complete"` → open the Download screen for that patient with the download button ready
- `notification_type: "pipeline_failed"` → open the Patient detail screen showing the error and a retry button

The push payload includes `patient_id`, `notification_type`, and `run_id`. Use these to navigate directly.

```dart
FirebaseMessaging.onMessageOpenedApp.listen((message) {
  final type = message.data['notification_type'];
  final patientId = message.data['patient_id'];
  
  if (type == 'review_needed') {
    navigateTo(ReviewScreen(patientId: patientId));
  } else if (type == 'pipeline_complete') {
    navigateTo(DownloadScreen(patientId: patientId));
  } else if (type == 'pipeline_failed') {
    navigateTo(PatientDetailScreen(patientId: patientId, showError: true));
  }
});
```

---

## 10. PDF Preloading (saves 1-2 seconds per download)

When the backend status changes to "complete," the app knows a PDF is ready. Don't wait for the caregiver to tap "Download."

As soon as status is "complete," call `GET /get-download-url/{patient_id}` in the background. Store the download URL. When the caregiver taps "Download," the URL is already fetched — the PDF loads instantly.

```dart
// When polling detects status == "complete"
final downloadUrl = await fetchDownloadUrl(patientId);  // pre-fetch in background
_cachedDownloadUrls[patientId] = downloadUrl;

// Later when caregiver taps "Download"
final url = _cachedDownloadUrls[patientId];  // already have it, no API call
openPdfViewer(url);  // instant
```

---

## Summary — What Each Optimization Saves

| # | Optimization | Time Saved |
|---|---|---|
| 1 | Live transcription | 25-30 sec per patient |
| 2 | Pre-fetch presigned URLs | 1-2 sec per upload |
| 3 | AAC instead of WAV | 1-3 sec per upload |
| 4 | Background audio upload | 2-3 sec per reassessment |
| 5 | Parallel API calls | 10-15 sec on app launch |
| 6 | Smart polling | Feels 2-3x more responsive |
| 7 | Local caching | Instant screen switches |
| 8 | Optimistic UI | No spinner waiting |
| 9 | Deep links | 3-5 sec per notification |
| 10 | PDF preloading | 1-2 sec per download |

**Total improvement: the app feels 30-40 seconds faster per patient interaction compared to a basic implementation.**

TRANSCRIBE_SPECIALTY=PRIMARYCARE
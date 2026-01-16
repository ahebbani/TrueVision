# TrueVision Subsystem Diagrams

These high-level flow diagrams summarize each subsystem at a similar level of detail to the facial recognition example. They are intended for audiences with minimal project knowledge.

## Facial Recognition

```mermaid
flowchart LR
    A[Face detected] --> B[Create facial embedding]
    B --> C[Load all person templates]
    C --> D{Nearest neighbor match<br/>distance under 0.6}
    D -- No --> E[Ignore]
    D -- Yes --> F[Return name, times seen, last seen]
    F --> G[Presence tracking]
    G --> H[Update last_seen, seen_count]
    H --> I{elapsed >= 120s}
    H --> M[Display driver]
    I -- Yes --> J[Add new template<br/>limit 30 per person]
    J --> K{templates over 30}
    J --> M
    K -- Yes --> L[Prune low quality]
    L --> M
```

## Audio Analysis

```mermaid
flowchart LR
    subgraph Capture & Input
        A[Microphone input] --> B[Chunk audio stream]
        B --> C{Mode}
    end

    C -- Live --> D[Live Caption]
    C -- Recordings --> E[Transcription]
    C -- Backfill --> F[Backfill Transcripts]

    D --> G[ASR engine - Whisper or Faster-Whisper]
    E --> G
    F --> G

    G --> H[Text segments]
    H --> I[Summarize Meetings]

    I --> J[Emit summaries/events]
    H --> K[Emit captions/transcripts]

    J --> L[Persist to DB]
    K --> L

    K --> M[Optional: Display captions]
    J --> N[Optional: Display summary]
```

## Data Access

```mermaid
flowchart LR
    A[Subsystem data requests] --> B[DB Access Layer]
    B --> C[(SQLite DB)]
    C --> D[Queries: faces, embeddings, audio, summaries]
    D --> E[Return records]
    E --> F[Visualization & Reports]
    F --> G[Generate HTML reports]
```

## Model Management

```mermaid
flowchart LR
    A[Startup or maintenance] --> B[Check required models]
    B --> C{Models present?}
    C -- No --> D[Fetch/Download models]
    D --> E[Verify integrity]
    C -- Yes --> E
    E --> F[Expose model paths to subsystems]
```

## OLED Output

```mermaid
flowchart LR
    A[Display requests<br/>name, status, captions, summary] --> B[Format content]
    B --> C[Render on OLED]
    C --> D[Test utilities]
    D --> C
```

## Main Orchestration

```mermaid
flowchart LR
    A[Initialize application] --> B[Configure dependencies<br/>models, DB, devices]
    B --> C[Start camera]
    B --> D[Start audio pipeline]
    B --> E[Start display]

    C --> F[Facial Recognition<br/>events: present, names, counts]
    D --> G[Audio Analysis<br/>events: captions, summaries]

    F --> H[Update DB]
    G --> H
    H --> I[Optional: Reports]

    F --> J[Update display]
    G --> J
    J --> K[User-visible output]
```

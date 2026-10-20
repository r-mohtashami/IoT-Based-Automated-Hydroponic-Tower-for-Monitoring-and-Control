# UX V2 — one-page home hydroponic control center

The Streamlit interface is designed around one home-user mental model:

**Home → My towers → Open a tower → See / manage / fix**

The visual design system from the previous interface is retained: color palette, typography, illustration assets, card styling, rounded geometry, and overall tone remain unchanged. UX V2 changes information architecture and interaction flow, not visual branding.

## One-page structure

There is no primary tab navigation for Dashboard, Towers, Plants, or AI Assistant.

The application has one main page containing:

1. the product header and system status;
2. an aggregate garden hero;
3. the tower grid;
4. a contextual **Add a tower** card;
5. a floating assistant launcher in the lower-right corner.

A plant is not treated as a separate top-level destination. Planting state, readings, recommendations, history, and plant management are all contextual to a Tower.

## Tower cards

Every Tower is represented by one clickable card. A card shows:

- user-facing Tower name;
- current active plant, or `No plant yet`;
- pH, EC, and water-level snapshot;
- telemetry age;
- exactly one primary status.

Primary statuses are intentionally limited to clear user-facing concepts such as:

- Everything looks good
- Needs your attention
- Waiting for your decision
- Ready to apply
- Checking the result
- Manual action needed
- Readings not updating
- Monitoring incomplete
- Ready for a plant

Stale telemetry is never presented as healthy simply because there is no active Alert.

## Tower dialog

Opening a Tower keeps the user on the same main page and opens a large scrollable dialog.

Information order is user-driven rather than database-driven:

1. current pH, EC, and water level;
2. **Needs your attention**;
3. active Planting or plant selection;
4. secondary readings;
5. trends;
6. user-facing History;
7. Technical details for debugging/demo evidence.

Database IDs, MQTT topics, internal rule codes, and raw event names remain hidden from the normal workflow.

## Attention and supervised corrections

Alert and Recommendation are separate backend concepts but are presented together as one user-facing attention flow.

The Human-in-the-Loop safety sequence remains:

**Detect → Explain → Recommend → Approve/Reject → Revalidate → Execute → Actuator ACK → New telemetry → Outcome verification**

Approval and execution are still separate actions. After approval, the UI explicitly states that nothing has been applied yet. Execution still performs backend revalidation before any MQTT command can be sent.

A Recommendation that is no longer valid because the condition recovered, changed direction, the Alert closed, or the Planting ended is not presented as actionable.

ACK remains distinct from physical recovery. After acknowledgement the UI enters **Checking the result** and waits for later telemetry.

## Manual EC-high handling

EC-high remains manual-only in the MVP.

The normal UI uses **Manual action needed** and explains that no automatic EC-high correction exists. It does not offer an Execute button and does not present the absence of an automatic actuator as a system failure.

Recovery is confirmed only by later EC telemetry.

## Plant management

Plant Profiles remain a backend/domain concept but are contextualized in the UI as Plants or Growing settings.

When selecting a plant, users can:

- use a protected evidence-backed built-in profile;
- make their own editable version of a protected built-in;
- use or edit an existing customized/custom plant;
- create a fully custom plant;
- create a Tower without starting a Planting.

Custom forms do not pre-fill invented agronomic targets. Users explicitly enter any custom values they want to configure. Application-owned units and backend validation remain authoritative.

An active Planting continues to use its saved profile snapshot. Later edits to a profile do not silently change an active Planting.

Ending a growing cycle closes its unresolved operational Alert/Recommendation state so it cannot leak into a later Planting. The UI asks whether the crop finished normally or was stopped early, then requires explicit confirmation. Ending the cycle clears the Tower for another plant but does not delete the saved Plant settings.

## Readings and trends

Missing sensor values are displayed as **Not monitored** and are never fabricated.

Factors with a sensor reading but no crop-specific target are presented as a **Tower sensor**, with direct copy explaining that the Tower measures the value while the current plant does not define a target range. Reservoir level is the clearest example: it is a device/tank reading, not a Basil/Lettuce crop target. Missing sensors remain **Not monitored**; these states are intentionally distinct.

Stale values are retained for context but clearly marked as old/not updating.

Trend charts use a data/target-aware vertical domain rather than forcing a zero baseline for values such as pH. Where the active Planting has a configured target, the chart also displays the target boundaries.

## History and technical evidence

Normal History uses plain-language events such as:

- You approved a correction
- Simulated device accepted the command
- Waiting for newer readings
- Back in target range
- Recommendation no longer needed
- Manual action required
- Action could not be confirmed

Raw event types, actuator identifiers, timestamps, and internal details are placed under a separate collapsed **Technical details** section.

## Add Tower flow

`+ Add a tower` opens a contextual wizard-style dialog rather than navigating to a separate setup page.

The user names the Tower and is then asked one simple question: choose a plant now, or set up the Tower only. Plant choice is explicit through named **Use ...** actions rather than an implicit select box. From the same guided dialog the user can:

- use an evidence-backed built-in plant;
- use an existing custom/customized plant;
- make an editable personal version of a built-in plant while preserving the protected original;
- create a custom plant with optional pH and/or EC targets;
- create the Tower without a Planting.

A custom plant may intentionally have no crop targets. In that case readings are still shown, but the UI explains that crop-specific alerts cannot be created until targets are configured.

Simulator mapping keys are generated internally and are not exposed in the normal UI. In normal Docker operation the simulator follows the backend Tower list, so a Tower added in the UI begins receiving simulated telemetry automatically and a removed Tower stops publishing after the next refresh. There is no hard-coded Tower-count limit.

## Tower lifecycle and saved-plant editing

A Tower dialog includes a collapsed **Tower settings** section. The user can rename the Tower or permanently remove it after explicit confirmation. Permanent removal deletes that Tower's local telemetry and operational history but leaves saved Plant settings available for reuse.

Built-in plant cards use **Use ...** and **Make my own ...** because built-ins are protected. User-owned/customized plant cards use **Use ...** and **Edit ...**. Saved-plant edits apply to future growing cycles only; an active Planting continues using its snapshot.

## Floating AI Assistant

The dedicated AI page is removed. A floating lower-right assistant button opens a chat dialog.

AI provider setup remains deferred until non-AI acceptance is complete. When unavailable, the assistant dialog clearly states that core monitoring, plant management, recommendation review, and supervised control continue to work without AI.

When enabled later, the Assistant must continue using controlled application tools only. It does not receive unrestricted SQL/MQTT access and cannot bypass application authority or user confirmation.

## Visual assets

Illustrations remain local under `streamlit/assets/` and require no external image URLs at runtime. Existing home, Tower, Planting, and Assistant imagery is reused in the new interaction structure.

# 13 · Application language for cover letters

Status: proposed. Implementation awaits user selection.

## Current behavior

Letter preparation detects the dominant language of an advert by word counts. Employers can explicitly ask for applications in a different language. [Source](../../../skill/ultron/scripts/ultron/cover_letter/brief.py).

## Expected behavior

Keep advert language and requested application language separate. Extract an explicit application instruction with its supporting phrase. Respect a user-selected output language and surface any conflict.

## Acceptance cases

- A Spanish advert saying 'envía tu candidatura en inglés' proposes an English letter and quotes the instruction in the brief.
- A requirement to speak English alone does not select English for the letter. Conflicting instructions surface uncertainty.

## Verification

Exercise monolingual, bilingual and conflicting adverts through brief preparation, including an explicit language override. Apply the [shared verification criteria](README.md#verification).

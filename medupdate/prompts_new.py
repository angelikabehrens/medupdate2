"""Neue Prompts für die automatisierten Schritte von medupdate 2 (in MyLuo gab es diese
Schritte nur als manuelle Eingaben)."""


def toc_prompt(toc_text: str) -> str:
    return f"""Du erhältst das Inhaltsverzeichnis eines Kapitels (oder mehrerer Kapitel) eines medizinischen \
Fortbildungshandbuchs. Zerlege es in die einzelnen Themen, zu denen jeweils aktuelle Publikationen \
recherchiert und kritisch gewürdigt werden sollen.

Regeln:
- Übernimm Kapitelnummer und Kapiteltitel, falls vorhanden.
- Jedes inhaltliche Unterthema wird ein Eintrag in "topics". Reine Strukturzeilen (z.B. "Einleitung", \
"Literatur", "Interessenkonflikte", Seitenzahlen) sind keine Themen.
- "title": der Thementitel wie im Inhaltsverzeichnis (ohne Nummer).
- "number": die Gliederungsnummer, falls vorhanden, sonst leer.
- "section_number" und "section_title": der Hauptabschnitt (oberste inhaltliche Gliederungsebene \
unterhalb des Kapitels, z.B. "1." / "2." oder "8.1" / "8.2"), zu dem das Thema gehört. Hat das \
Inhaltsverzeichnis keine Unterthemen, ist jedes Thema sein eigener Hauptabschnitt.
- "subsection_number" und "subsection_title": nur wenn das Inhaltsverzeichnis zwischen Hauptabschnitt \
und Thema eine weitere Gliederungsebene hat (z.B. Hauptabschnitt "8.2", Unterabschnitt "8.2.7", \
Thema "8.2.7.1"): der Unterabschnitt, zu dem das Thema gehört. Sonst beide leer.
- "english_search_focus": eine präzise englische Fachbeschreibung des Themas (5–15 Wörter), \
geeignet als Ausgangspunkt für eine PubMed-Recherche.
- Erfinde keine Themen, die nicht im Inhaltsverzeichnis stehen.

Inhaltsverzeichnis:
{toc_text}"""


def author_rules_prompt(guide_text: str) -> str:
    return f"""Du erhältst die "Hinweise für Autoren" eines medizinischen Fortbildungshandbuchs. Extrahiere \
daraus alle Vorgaben, die für das automatische Erstellen eines Manuskripts relevant sind.

Regeln:
- Übernimm nur, was tatsächlich in den Hinweisen steht. Fehlt eine Angabe, gib 0 (für Zahlen) bzw. \
einen leeren Text zurück – erfinde keine Vorgaben.
- "citation_style": Beschreibung des geforderten Zitierstils für das Literaturverzeichnis \
(Autorenzahl, "et al."-Regel, Reihenfolge der Elemente, Satzzeichen, Kursivsetzung, DOI/PMID).
- "citation_examples": wörtlich übernommene Beispiel-Literaturangaben aus den Hinweisen (falls vorhanden).
- "intext_citation_style": wie im Fließtext zitiert wird (z.B. "Ziffern in runden Klammern").
- "recency_months": wie alt die besprochenen Publikationen höchstens sein sollen, in Monaten \
(z.B. "nicht älter als ein Jahr" = 12).
- "max_references", "max_pages", "min_figures", "max_figures": Zahlen laut Hinweisen.
- "pubs_per_topic": Anzahl der zu besprechenden Publikationen pro Thema, falls angegeben.
- "length_rules": Vorgaben zur Länge einzelner Publikationsbesprechungen.
- "structure_rules": Vorgaben zu Aufbau und Abschnitten (z.B. Abstract, Kommentar, Fazit, State of the Art).
- "language_rules": sprachliche Vorgaben (z.B. Wirkstoffnamen, Abkürzungen, Gendern, Rechtschreibung).
- "figure_rules": Vorgaben zu Abbildungen/Tabellen und deren Quellenangaben.
- "other_rules": alle weiteren relevanten Vorgaben als kurze Stichpunkte.

Hinweise für Autoren:
{guide_text}"""


def search_plan_prompt(topic: str, focus: str, chapter: str, von: str, bis: str) -> str:
    return f"""Du bist Informationsspezialist für medizinische Literaturrecherche (PubMed/MEDLINE) und \
bereitest die Literaturauswahl für ein ärztliches Fortbildungshandbuch vor. Gesucht werden NEUE, \
klinisch relevante Originalarbeiten und hochwertige Übersichtsarbeiten, die seit {von} erschienen sind.

Kapitel: {chapter or "(nicht angegeben)"}
Thema: {topic}
Fachlicher Suchfokus (englisch): {focus}

Aufgabe:
1. Leite eine PICO-Struktur (Population, Intervention/Exposition, Vergleich, Endpunkte) für das Thema ab. \
Ist das Thema breit (z.B. eine ganze Erkrankung), beschreibe die klinisch wichtigsten Fragestellungen.
2. Formuliere genau drei PubMed-Suchstrategien, die sich ergänzen:
   - "praezise": eng auf das Thema, MeSH-Terms ([Mesh]) UND Freitext ([tiab]) mit Synonymen kombiniert.
   - "breit": sensitiver, mehr Synonyme und verwandte Begriffe, damit wichtige Arbeiten nicht fehlen.
   - "hohe_evidenz": wie "praezise", aber eingeschränkt auf randomisierte Studien, Metaanalysen und \
systematische Reviews (z.B. randomized controlled trial[pt] OR meta-analysis[pt] OR systematic[sb]).
3. Schreibe ALLE Suchbegriffe auf ENGLISCH (PubMed indexiert englisch): deutsche Themenbegriffe \
übersetzen (z.B. "Fundusvarizen" → "gastric varices" / "fundic varices", MeSH "Esophageal and Gastric \
Varices"[Mesh]). Verwende nur MeSH-Begriffe, die es tatsächlich gibt; im Zweifel Freitext [tiab].
   Verwende korrekte PubMed-Syntax: Feld-Tags, OR innerhalb eines Konzepts, AND zwischen Konzepten, \
Klammern. KEINE Datumsangaben und KEINE Sprachfilter in die Suchstrings schreiben – das ergänzt die App.
4. Nenne Ausschlusskriterien (z.B. Tierstudien, pädiatrische Population, wenn nicht gemeint).

Hinweise: Bevorzuge Begriffe, wie sie in Titeln und Abstracts aktueller klinischer Studien vorkommen. \
Vermeide zu viele AND-Verknüpfungen, die die Trefferzahl auf null drücken: höchstens zwei bis drei \
Konzepte mit AND verknüpfen; "breit" am besten nur das Kernkonzept mit allen Synonymen."""


def search_broaden_prompt(topic: str, focus: str, failed: str) -> str:
    return f"""Die folgenden PubMed-Suchstrategien zum Thema „{topic}“ (Suchfokus: {focus}) haben zusammen \
zu wenige Treffer geliefert:

{failed}

Formuliere drei deutlich SENSITIVERE Suchstrategien auf Englisch: nur das Kernkonzept des Themas mit \
allen gängigen englischen Synonymen und Schreibweisen als Freitext ([tiab]), ODER-verknüpft, optional ein \
passender MeSH-Begriff; höchstens ein weiteres Konzept mit AND. Keine Datums- oder Sprachfilter."""


def ranking_prompt(topic: str, pico: str, candidates_block: str, recency_text: str) -> str:
    return f"""Du bist ein wissenschaftlicher Literatur- und Evidenzanalyse-Assistent mit Expertise in \
evidenzbasierter Medizin, klinischer Epidemiologie, Studiendesign, Biostatistik, Risk-of-Bias-Bewertung und \
PICO-Fragestellungen. Du wählst Publikationen für ein ärztliches Fortbildungshandbuch (med update) aus. \
Das Handbuch bespricht für Fachärztinnen und Fachärzte die wichtigsten NEUEN Arbeiten {recency_text} und \
beantwortet zu jeder Arbeit die Leitfrage: Ist das Ergebnis neu? Ist es valide? Ändert es die Praxis?

Thema: {topic}
PICO / Fragestellungen: {pico}

Bewerte JEDE der unten stehenden Publikationen anhand von Titel und Abstract. Übernimm NICHT die \
PubMed-Reihenfolge und lass Journal oder Impact Factor NICHT in "relevance", "evidence", "novelty", \
"practice_impact" oder "question_importance" einfließen – dafür gibt es das eigene Feld \
"journal_impact_factor".

Für jede Publikation:
- "study_design": tatsächliches Design (nicht nur den PubMed-Publication-Type übernehmen); wenn unklar: \
"nicht eindeutig beurteilbar".
- "relevance" (0–100): Passung zu Thema und PICO. Arbeiten zu einer anderen Population, einem anderen \
Organ oder einer reinen Laborfragestellung erhalten höchstens 30.
- "evidence" (0–100): methodische Qualität und Evidenzstärke, designgerecht (Fallzahl, Randomisierung, \
Kontrollgruppe, Endpunkte, Präzision). Statistische Signifikanz nicht mit klinischer Relevanz verwechseln.
- "risk_of_bias": Low / Some concerns / High / Unclear (im Abstract fehlende Angaben sind "Unclear", \
nicht "Low").
- "novelty" (0–100): Liefert die Arbeit bisher nicht bekannte Erkenntnisse (erste RCT, erstmals harte \
Endpunkte, neue Population, widerspricht bisheriger Evidenz) oder bestätigt sie nur Bekanntes?
- "practice_impact" (0–100): Wie wahrscheinlich ändert das Ergebnis Diagnostik oder Therapie in der \
klinischen Routine in Deutschland?
- "question_importance" (0–100): Bedeutung der untersuchten Fragestellung für das Themengebiet insgesamt \
(häufige Erkrankung, harte Endpunkte, offene Kontroverse, Leitlinienrelevanz) – unabhängig von der \
Qualität der Studie.
- "journal_impact_factor": der aktuelle Journal Impact Factor des Journals, soweit bekannt (Zahl, z.B. 96.2); \
0, wenn unbekannt. Nicht raten – im Zweifel 0.
- "question_key": die konkrete klinische Fragestellung in 3–8 englischen Wörtern, so normiert, dass \
Arbeiten zur GLEICHEN Fragestellung den GLEICHEN Schlüssel erhalten (z.B. "semaglutide cardiovascular \
outcomes obesity").
- "is_primary_research": true bei Originalarbeit (inkl. Metaanalyse), false bei narrativem Review, \
Leitlinie, Editorial, Konsensus, Kommentar, Protokoll ohne Ergebnisse.
- "rationale": ein Satz, warum die Arbeit (nicht) in das Handbuch gehört.

Wichtige Prinzipien: Keine Daten erfinden, Unsicherheit ausdrücklich ausweisen, RCTs nicht automatisch \
als gute Studie werten, Studienprotokolle ohne Ergebnisse und reine Machbarkeitsstudien niedrig bewerten.

Publikationen:
{candidates_block}"""


def guideline_search_prompt(topic: str, chapter: str, today: str) -> str:
    return f"""Recherchiere mit der Websuche die AKTUELL GÜLTIGEN Leitlinien zum folgenden Thema eines \
deutschsprachigen ärztlichen Fortbildungshandbuchs (Stand heute: {today}).

Kapitel: {chapter or "(nicht angegeben)"}
Thema: {topic}

Suche gezielt nach:
1. deutschen Leitlinien (AWMF-Register, S3/S2k/S2e/S1, Nationale VersorgungsLeitlinien),
2. europäischen Leitlinien der zuständigen Fachgesellschaften (z.B. ESC, ESMO, EASL, ESGE, ERS, EULAR, ESPEN),
3. internationalen Leitlinien (z.B. ACC/AHA, ASCO, NCCN, AGA, ACG, ADA, KDIGO, NICE, WHO),
4. aktuellen Konsensus- oder Positionspapieren, falls es keine Leitlinie gibt.

Prüfe bei jeder Leitlinie, ob sie die aktuelle Fassung ist (nicht abgelaufen, keine neuere Version, \
Gültigkeitsdatum) und ob sie frei zugänglich ist. Suche nach Möglichkeit den direkten Link zur \
Volltext-PDF (z.B. register.awmf.org/.../XXX_S3_..._2024-05.pdf oder die Verlagsseite mit Open Access).

Gib am Ende eine Liste der höchstens 6 wichtigsten Leitlinien aus, jeweils mit: Titel, herausgebende \
Fachgesellschaft(en), Jahr der aktuellen Fassung, Registernummer (falls AWMF), Link zur Übersichtsseite, \
Link zur Volltext-PDF (falls gefunden), frei zugänglich ja/nein, und warum sie für das Thema relevant ist. \
Erfinde keine Leitlinien, Links oder Jahreszahlen – nenne nur, was du in der Recherche tatsächlich gefunden hast."""


def guideline_extract_prompt(research_text: str) -> str:
    return f"""Übertrage die folgende Leitlinienrecherche in die strukturierte Form. Übernimm nur \
Angaben, die im Text stehen; fehlende Angaben bleiben leer. Links nur exakt so übernehmen, wie sie im \
Text stehen.

Recherche:
{research_text}"""


def identify_pub_prompt(head_text: str) -> str:
    return f"""Der folgende Text ist der Anfang einer wissenschaftlichen Publikation (aus einer PDF \
extrahiert). Bestimme die bibliografischen Angaben. Übernimm nur, was im Text steht; fehlende \
Angaben bleiben leer. "authors": Nachname und Initialen ohne Punkte (z.B. "Müller AB"), höchstens die \
ersten sechs. "kind": "original" für Originalarbeiten/Metaanalysen, "review", "guideline" oder "other".

Text:
{head_text}"""


def topic_assign_prompt(topics_block: str, pubs_block: str) -> str:
    return f"""Ordne jede Publikation genau einem Thema des Handbuchkapitels zu. Wähle das Thema, zu \
dem die Publikation inhaltlich am besten passt. Passt sie zu keinem Thema, gib die Themen-ID "" zurück.

Themen:
{topics_block}

Publikationen:
{pubs_block}"""


def figure_pick_prompt(title: str, n_images: int) -> str:
    return f"""Die {n_images} Bilder stammen aus der Publikation „{title}“. Wähle das EINE Bild, das die \
Hauptaussage der Studie am besten zeigt (z.B. Kaplan-Meier-Kurve oder Forest-Plot zum primären Endpunkt, \
zentrale Ergebnistabelle, Studiendesign-Schema). Logos, Verlagsgrafiken, Autorenfotos, Formeln oder \
unleserlich kleine Bilder sind ungeeignet. Ist kein Bild geeignet, gib index = -1 zurück.

"index": Nummer des Bildes (beginnend bei 0, in der Reihenfolge der Bilder).
"typ": "Tabelle" nur bei einer echten Datentabelle, sonst "Abbildung".
"caption_en" und "caption_de": sachliche Bildunterschrift, höchstens 15 Wörter, nur was tatsächlich zu \
sehen ist (z.B. "Kaplan-Meier-Kurve zum Gesamtüberleben nach Behandlungsarm")."""


def translate_structured_prompt(text: str) -> str:
    return f"""Übersetze die folgende gekürzte kritische Würdigung einer klinischen Studie vollständig ins \
Deutsche, präzise und fachsprachlich korrekt, in einem sachlich-akademischen Stil, wie ihn eine erfahrene \
Autorin in einem deutschsprachigen Fortbildungshandbuch schreibt.

Labels am Absatzanfang exakt so übersetzen: "Study:" -> "Studie:", "Results:" -> "Ergebnisse:", \
"Authors' Conclusion:" -> "Schlussfolgerung der Autoren:", "Comment:" -> "Kommentar:", \
"Conclusion for Practice:" -> "Fazit für die Praxis:".

Inhaltliche Treue hat Vorrang vor allem anderen: nichts hinzufügen, nichts weglassen; Zahlen, Effektgrößen, \
Konfidenzintervalle und p-Werte unverändert (Dezimalpunkt wird zum deutschen Dezimalkomma, sonst keine \
Änderung); Hedging weder abschwächen noch verstärken; keine Kausalität, wo eine Assoziation beschrieben \
ist; Fachbegriffe und etablierte Abkürzungen beibehalten. Arzneimittel nur mit Wirkstoffnamen. \
"CI" wird im Fließtext zu "Konfidenzintervall", in Zahlenklammern zu "95%-KI". Keine Ich-/Wir-Form.

Klammerverweise wie "(1)" oder "(2,3)" bleiben exakt an derselben Stelle im Satz stehen. Absatzstruktur \
beibehalten (eine Zeile pro Absatz). Gib NUR die Übersetzung aus, ohne Einleitung oder Kommentar.

Text:
{text}"""


def style_profile_prompt(samples: list[str]) -> str:
    joined = "\n\n---\n\n".join(f"Beispieltext {i + 1}:\n{s}" for i, s in enumerate(samples))
    return f"""Analysiere den persönlichen Schreibstil der Autorin bzw. des Autors in den folgenden, \
früher veröffentlichten Manuskripten eines ärztlichen Fortbildungshandbuchs. Beschreibe ausschließlich \
STILISTISCHE Merkmale, keine Inhalte: typische Satzlänge und Satzbau, bevorzugte Übergänge und \
Konnektoren, Grad der Direktheit und Wertung, Umgang mit Zahlen und Abkürzungen, typische \
Formulierungen bei Einordnung, Kommentar und Fazit, Fachsprache vs. erklärende Sprache, Tempus, \
Aktiv/Passiv. Nenne zu jedem Merkmal 1–2 kurze wörtliche Beispielformulierungen (höchstens 8 Wörter, \
ohne Studieninhalte). Höchstens 300 Wörter, als Stichpunkte.

{joined}"""


def style_apply_prompt(text: str, profile: str, sample_excerpt: str) -> str:
    return f"""Du bist wissenschaftliche Redakteurin. Passe den folgenden deutschen Text einer \
Studienbesprechung an den persönlichen Schreibstil der Autorin bzw. des Autors an.

Stilprofil:
{profile}

Kurzer Auszug aus einem früheren Manuskript (NUR zur Stilorientierung – behandelt eine andere Studie, \
keine Inhalte, Zahlen oder Bewertungen daraus übernehmen):
{sample_excerpt}

Oberste Regel: Inhaltstreue
- Verändere ausschließlich Wortwahl, Satzbau, Übergänge und Tonfall.
- Keine neuen Informationen, keine weggelassenen Aussagen, keine Verschiebung von Richtung oder Stärke \
einer Aussage, Hedging bleibt erhalten, keine Kausalität aus einer Assoziation.
- Alle Zahlen, Effektgrößen, Konfidenzintervalle, p-Werte und Klammerverweise wie "(1)" oder "(2,3)" \
bleiben exakt erhalten.
- Absatzstruktur und Labels am Absatzanfang (z.B. "Studie:", "Ergebnisse:", "Schlussfolgerung der \
Autoren:", "Kommentar:", "Fazit für die Praxis:") bleiben unverändert erhalten; eine Zeile pro Absatz.
- Länge ungefähr gleich (±10 %).
- Gib NUR den überarbeiteten Text aus.

Text:
{text}"""


def reference_format_prompt(style: str, examples: str, entries_block: str) -> str:
    return f"""Formatiere die folgenden Literaturangaben exakt nach dem Zitierstil der "Hinweise für \
Autoren". Ändere ausschließlich Reihenfolge, Satzzeichen, Abkürzungen ("et al."), Groß-/Kleinschreibung \
der Elemente und die Darstellung von DOI/PMID – NIEMALS Inhalte: keine Autoren, Titel, Jahreszahlen, \
Bände, Seiten oder Kennungen ergänzen, entfernen oder verändern. Fehlt ein vom Stil verlangtes Element, \
lasse es weg, statt es zu erfinden. Gib jede Angabe ohne Nummer zurück, in derselben Reihenfolge.

Zitierstil laut Hinweisen für Autoren:
{style}

Beispiele aus den Hinweisen für Autoren:
{examples or "(keine Beispiele angegeben)"}

Literaturangaben:
{entries_block}"""


def citation_fix_prompt(text: str, lit_label: str, listed: list[int], cited: list[int], sprache_instr: str) -> str:
    """Aus MyLuo (fix_citation_consistency) übernommen."""
    return f"""Im folgenden Text gibt es eine Inkonsistenz zwischen den im Abschnitt "{lit_label}" \
aufgeführten Quellennummern ({listed if listed else 'keine'}) und den tatsächlich im Fließtext von \
Abschnitt 4 verwendeten Klammerverweisen ({cited if cited else 'keine'}). {sprache_instr}

Korrigiere dies:
- Falls eine Nummer in der Literaturliste steht, aber im Text nicht als Klammerverweis vorkommt: Ergänze im \
Fließtext von Abschnitt 4 an einer inhaltlich passenden Stelle den fehlenden Klammerverweis (direkt vor dem \
Satzpunkt). Wenn mehrere aufeinanderfolgende Sätze eindeutig dieselbe Publikation zusammenfassen, reicht ein \
Klammerverweis am Ende des letzten dieser Sätze.
- Falls eine Nummer im Text als Klammerverweis vorkommt, aber nicht in der Literaturliste steht: Ergänze den \
fehlenden Eintrag in der Literaturliste.
- Falls eine Nummer in der Literaturliste steht, aber im Text gar nicht mehr benötigt wird: Entferne den Eintrag \
und nummeriere ggf. neu durch (Text und Liste müssen dabei konsistent bleiben).

Verändere ansonsten den Inhalt und die Formulierungen des Textes nicht – füge ausschließlich die fehlenden \
Klammerverweise bzw. Literaturlisten-Einträge hinzu oder entferne nicht mehr benötigte Einträge.

Gib den KOMPLETTEN, korrigierten Text zurück – alle Abschnitte 1-5, Kommentar/Comment, \
Fazit für die Praxis/Conclusion for Practice und \
{lit_label} (falls vorhanden) – im unveränderten Format und derselben Sprache wie unten, ohne zusätzliche \
Erklärungen davor oder danach.

Text:
{text}"""


def sota_from_intro_prompt(topic: str, pub_title: str, intro: str, references: str) -> str:
    return f"""Erstelle für das Thema „{topic}“ einen kurzen Abschnitt „State of the Art“ auf englischer \
Sprache. Er steht im Manuskript direkt vor der Besprechung der folgenden Publikation und leitet auf \
das Thema hin.

Grundlage ist die Einleitung dieser Publikation („{pub_title}“). Wähle daraus 3 bis 5 Kernaussagen, \
die den Leser auf das Thema hinführen und es in den Gesamtkontext einordnen: aktuelle Bedeutung des \
Problems, etablierter Standard bzw. Wissensstand und die offene Frage oder Evidenzlücke, an der die \
Studie ansetzt.

Regeln:
- 3 bis 5 Sätze, insgesamt ca. 70 bis 110 Wörter, jeder Satz höchstens ca. 30 Wörter.
- Formuliere die Aussagen eigenständig und fachsprachlich um; übernimm keine Sätze wörtlich.
- Nimm keine Ergebnisse oder Schlussfolgerungen der Studie vorweg und nenne die Studie selbst nicht.
- Zahlen nur, wenn sie so in der Einleitung stehen. Erfinde nichts hinzu.
- Arzneimittel nur mit Wirkstoffnamen. Keine Markdown-Formatierung.

Literaturverweise:
- Belege die Aussagen mit den Quellen, die die Einleitung an dieser Stelle selbst zitiert, als \
nummerierte Klammerverweise vor dem Satzpunkt („… etabliert (1).“), neu nummeriert ab 1 in der \
Reihenfolge des ersten Auftretens. Höchstens 5 Quellen; mehrere Quellen zu einer Aussage als „(1,2)“.
- Übernimm die Angaben dieser Quellen aus dem Literaturverzeichnis der Publikation (unten). Format: \
Nachname Initialen, Nachname Initialen, Nachname Initialen. Titel. Journal. Jahr;Band:Seiten. Bei mehr als \
drei Autoren die ersten drei, dann „et al.“. Fehlende Angaben weglassen, nichts ergänzen.
- Lässt sich eine Quelle im Literaturverzeichnis nicht eindeutig finden, formuliere die Aussage ohne Verweis.

Ausgabe: die Überschrift „State of the Art“, darunter der Fließtext, darunter die Überschrift \
„References“ mit der nummerierten Liste. Keine weitere Erläuterung.

Einleitung der Publikation:
{intro}

Literaturverzeichnis der Publikation (Auszug):
{references}"""

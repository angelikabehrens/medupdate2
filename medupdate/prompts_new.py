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
dem die Publikation inhaltlich am besten passt. Passt sie inhaltlich zu keinem Thema wirklich, gib die \
Themen-ID "" zurück; sie erscheint dann im Manuskript unter „Varia“.

Themen:
{topics_block}

Publikationen:
{pubs_block}"""


def reassign_prompt(chapter: str, topics_block: str, pubs_block: str) -> str:
    return f"""Für das Handbuchkapitel „{chapter}“ liegen alle Publikationen vor, die im Manuskript gewürdigt \
werden. Jede ist vorläufig einem Thema des Inhaltsverzeichnisses zugeordnet (aus der Literatursuche oder beim \
Hochladen). Prüfe die Zuordnung im Gesamtblick und ordne jede Publikation dem Thema zu, zu dem ihr \
inhaltlicher SCHWERPUNKT gehört.

Regeln:
- Maßgeblich ist die eigentliche Fragestellung der Arbeit, nicht ein Stichwort im Titel. Beispiel: Eine \
Arbeit zur endoskopischen Resektion von Barrettkarzinomen mit klinischer Fragestellung (z.B. Rezidiv, \
Überleben, Indikation) gehört zum Thema Barrettkarzinom. Untersucht sie einen technischen Aspekt der \
Resektion (z.B. Technikvergleich, Resektionsgerät, En-bloc-Rate), gehört sie zum Thema endoskopische Resektion.
- Bevorzuge das spezifischste passende Thema. Die bisherige Zuordnung nur ändern, wenn ein anderes Thema \
klar besser passt.
- Publikationen, die nahezu dieselbe klinische Fragestellung bearbeiten (gleiche Population/Indikation, \
vergleichbare Endpunkte, z.B. verschiedene endoskopische Techniken zur Therapie derselben Indikation), \
werden später gemeinsam gewürdigt. Sie MÜSSEN demselben Thema zugeordnet werden. Nenne sie in \
"same_question" (2 oder 3 je Gruppe, jede Publikation höchstens in einer Gruppe; dasselbe Thema allein \
reicht nicht, im Zweifel keine Gruppe).
- topic_id "" (= Abschnitt „Varia“) ist nur für Publikationen mit dem Vermerk [hochgeladen] erlaubt, die \
zu keinem Thema wirklich passen. Alle anderen bekommen ein Thema aus der Liste.
- "reason_de": nur bei einer geänderten Zuordnung ein kurzer Satz (höchstens 20 Wörter), warum; sonst "".
- "question_de": die gemeinsame Fragestellung als kurzer Titel (höchstens ca. 12 Wörter).

Inhaltsverzeichnis (Themen-ID: Nummer Titel):
{topics_block}

Publikationen (ID | bisheriges Thema | Titel | Fragestellung | Inhalt):
{pubs_block}"""


def headings_de_prompt(items: str) -> str:
    return f"""Übersetze die folgenden Überschriften eines medizinischen Fortbildungshandbuchs \
(Gastroenterologie/Endoskopie) ins Deutsche, wie sie in einem deutschen Fachbuch stehen würden: \
fachsprachlich korrekt, knapp, gleiche Großschreibung wie im Deutschen üblich. Etablierte Abkürzungen \
(z.B. GERD, EUS, ERCP, ESD) bleiben erhalten. Ist eine Überschrift schon deutsch, gib sie unverändert \
zurück. "id" ist die Nummer vor dem Doppelpunkt.

{items}"""


def vary_fazit_prompt(items: str) -> str:
    return f"""Ein deutsches Fortbildungsmanuskript enthält viele Studienbesprechungen. Ihr Absatz "Fazit für \
die Praxis" beginnt oft mit demselben Satz (z.B. "Das Ergebnis ist neu."). Formuliere für jede \
Besprechung NUR den ersten Satz des Fazits neu, damit sich die Formulierungen im Manuskript abwechseln \
und konkret zur Studie passen, z.B. "Diese Studie zeigt erstmals, dass …", "Erstmals liegt … vor", "Die \
Arbeit bestätigt …", "Neu ist vor allem …", "Die Daten stützen …".

Regeln:
- Die Aussage bleibt gleich (neu / bestätigend / nicht neu; Hedging bleibt). Keine neuen Fakten: Was neu \
ist, darfst du nur aus dem mitgegebenen Fazit-Absatz übernehmen.
- Der Satz muss nahtlos vor den übrigen Sätzen des Absatzes stehen können; wiederhole nichts, was im \
nächsten Satz ohnehin folgt.
- Höchstens ca. 25 Wörter, keine Ich-/Wir-Form, keine Klammerverweise hinzufügen oder entfernen.
- Keine zwei Besprechungen mit demselben Satzanfang.
- "id" wie vorgegeben, "sentence": der neue erste Satz.

{items}"""


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
"Conclusion for Practice:" -> "Fazit für die Praxis:", "Overview of the Publications" -> "Übersicht der \
Publikationen", "Publication 1:" -> "Publikation 1:" (ebenso 2, 3), "Assessment of the Linkage" -> \
"Einordnung der Verknüpfung", "Critical Appraisal" -> "Kritische Würdigung", "Joint Conclusion:" -> \
"Gemeinsames Fazit:".

Inhaltliche Treue hat Vorrang vor allem anderen: nichts hinzufügen, nichts weglassen; Zahlen, Effektgrößen, \
Konfidenzintervalle und p-Werte unverändert (Dezimalpunkt wird zum deutschen Dezimalkomma, sonst keine \
Änderung); Hedging weder abschwächen noch verstärken; keine Kausalität, wo eine Assoziation beschrieben \
ist; Fachbegriffe und etablierte Abkürzungen beibehalten. Arzneimittel nur mit Wirkstoffnamen. \
"CI" wird im Fließtext zu "Konfidenzintervall", in Zahlenklammern zu "95%-KI". Keine Ich-/Wir-Form.

Klammerverweise wie "(1)" oder "(2,3)" bleiben exakt an derselben Stelle im Satz stehen. Absatzstruktur \
beibehalten (eine Zeile pro Absatz). Schreibe gut lesbare deutsche Sätze: Einen langen, verschachtelten \
Satz darfst du in zwei kürzere Sätze teilen, ohne Inhalt wegzulassen. Gib NUR die Übersetzung aus, ohne \
Einleitung oder Kommentar.

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
- Kurze, klare Sätze (möglichst höchstens 20–25 Wörter), keine Schachtelsätze, auch wenn die \
Beispieltexte längere Sätze haben. Verschachtelte Sätze in zwei Sätze teilen.
- Methodik und Statistik verständlich: Fachbegriffe (z.B. Impräzision, Entblindung, Lernkurveneffekt, \
Baseline-Imbalance, Kovariatenadjustierung, explorativ) kurz im Halbsatz erklären oder in allgemein \
verständliche Worte fassen; bei jeder Limitation sagen, was sie für das Ergebnis bedeutet (in welche \
Richtung sie es verschieben würde); Stichwortketten nach einem Doppelpunkt in flüssige Sätze auflösen. \
Die statistische Würdigung in 2–3 einfachen Sätzen (ausnahmsweise 4, wenn sonst Inhalt verloren \
ginge), danach genau ein Satz zur Qualität der Studie \
aus statistischer Sicht (z.B. "Aus statistischer Sicht ist die Studie gut belastbar, weil …"). Keine \
Metabemerkungen über die Quelle ("im Abstract nicht angegeben"). Schlussfolgerung der Autoren und Fazit im Indikativ, ohne Konjunktivketten; keine angehängten \
Satzfragmente. Nicht gendern (generisches Maskulinum, keine Sternchen oder Doppelnennungen). \
Dabei nichts weglassen und keine neue Bewertung hinzufügen.
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
„References“ mit der nummerierten Liste. Nach der Liste folgt nichts mehr: keine Hinweise, \
Vorbehalte oder Prüfbitten.

Einleitung der Publikation:
{intro}

Literaturverzeichnis der Publikation (Auszug):
{references}"""


def own_refs_prompt(manuscripts: list[tuple[str, str]], pubs: list[dict], max_refs: int) -> str:
    ms = "\n\n".join(f"=== Früheres Manuskript „{name}“ ===\n{text}" for name, text in manuscripts)
    ps = "\n".join(f"- id {p['id']} | Thema: {p['topic']} | {p['title']} | {p['summary']}" for p in pubs)
    return f"""Du unterstützt die Autorin eines jährlich erscheinenden ärztlichen Fortbildungshandbuchs. Unten \
stehen ihre früheren Manuskripte und die Publikationen, die sie im neuen Manuskript bespricht.

Suche höchstens {max_refs} Stellen, an denen ein inhaltlicher Rückbezug auf ein früheres Manuskript \
für die Leser wirklich sinnvoll ist: Das frühere Manuskript hat eine Publikation oder ein Ergebnis zur \
selben klinischen Frage besprochen, und die neue Publikation bestätigt, ergänzt, widerlegt oder führt \
es weiter. Lieber keinen Rückbezug als einen gezwungenen. Gibt es keinen sinnvollen, gib eine leere \
Liste zurück.

Je Rückbezug:
- "pub_id": die id der neuen Publikation (aus der Liste unten),
- "manuscript": der Name des früheren Manuskripts,
- "sentence_de": EIN deutscher Satz (höchstens ca. 35 Wörter) im Stil der Autorin, z.B. „Im Jahr 2025 \
hatten wir zu den Ösophagusvarizen die Studie von Müller et al. vorgestellt, die … zeigte; die \
vorliegende Arbeit ergänzt nun …“. Verwende die Wir-Form wie im Beispiel.
- "sentence_en": derselbe Satz auf Englisch.

Strenge Regeln: Übernimm Jahr, Studie und Ergebnis des früheren Manuskripts nur so, wie sie dort stehen. \
Steht das Jahr nicht eindeutig im Manuskript, schreibe „In einem früheren Beitrag“ statt einer \
Jahreszahl. Erfinde nichts. Über die neue Publikation nur sagen, was in ihrer Zusammenfassung unten steht. \
Keine Literaturverweise in Klammern.

Neue Publikationen:
{ps}

{ms}"""


def group_prompt(topic: str, pubs: list[dict], hints: list[list[str]] | None = None) -> str:
    ps = "\n".join(f"- id {p['id']} | {p['title']} | Fragestellung: {p['question']} | {p['summary']}" for p in pubs)
    if hints:
        ps += ("\n\nBei der Prüfung der Themenzuordnung wurden diese Publikationen als nahezu gleiche Fragestellung "
               "erkannt (bitte bestätigen oder verwerfen): " + "; ".join(" + ".join(h) for h in hints))
    return f"""Du erhältst die Publikationen, die im Manuskript zum Thema „{topic}“ besprochen werden. Prüfe, \
ob zwei oder drei davon nahezu dieselbe klinische Fragestellung bearbeiten, z.B. verschiedene \
endoskopische Techniken zur Therapie derselben Indikation oder zwei Studien unterschiedlicher \
Evidenzstufe zur selben Frage. Solche Publikationen werden im Manuskript gemeinsam gewürdigt statt \
einzeln nacheinander.

Regeln:
- Eine Gruppe hat 2 oder 3 Publikationen; jede Publikation gehört höchstens zu einer Gruppe.
- Nur bei wirklich nahezu gleicher Fragestellung (gleiche Population/Indikation und vergleichbare \
Endpunkte). Dasselbe Thema allein reicht nicht. Im Zweifel keine Gruppe.
- "question_de" / "question_en": die gemeinsame Fragestellung als kurzer Titel (höchstens ca. 12 Wörter).
- Gibt es keine solche Gruppe, gib eine leere Liste zurück.

Publikationen:
{ps}"""


def joint_appraisal_prompt(question: str, members: list[dict]) -> str:
    blocks = "\n\n".join(
        f"--- Publication {i} ---\nTitle: {m['title']}\nJournal: {m['journal']} ({m['year']})\n"
        f"Study type: {m['pub_types']}\nExisting critical appraisal of this publication:\n{m['appraisal']}"
        for i, m in enumerate(members, start=1))
    return f"""You are an experienced clinical scientist writing for a German continuing medical education \
handbook (med update) for specialists. Write in English. Create ONE joint, comparative critical appraisal of the \
following {len(members)} publications on the shared clinical question: "{question}".

Do NOT appraise the publications one after another. Relate them to each other. Work ONLY with the \
existing appraisals below (they were written from the full texts). Do not invent data, methods, results or \
sources; do not round or change numbers; keep the authors' conclusions with their hedging. Drugs only by \
generic name. No Markdown.

Use exactly these sections, each heading on its own line:

Overview of the Publications
One short block per publication, starting with "Publication 1:", "Publication 2:" etc.: design and \
evidence level, population, intervention/comparison, main result with the key numbers.

Assessment of the Linkage
Can the publications be considered together, and how? Are populations comparable? Same or different \
evidence level? May effect estimates be placed side by side or combined, or not? Do they complement or \
contradict each other? End with a clear statement of what kind of linkage is valid and what is not \
(e.g. no pooled effect estimate).

Critical Appraisal
Running text: methodological strengths and weaknesses across the publications, where they confirm, \
qualify or differ from each other, and the relevance for clinical practice in the context of guidelines.

Joint Conclusion
2-4 sentences answering the shared question. Weigh the different evidence quality explicitly.

References
External sources cited in the text, taken unchanged from the reference lists of the existing appraisals, \
numbered (1), (2), ... in order of first citation; cite them in the text as "(1)", "(2,3)". Do NOT give the \
{len(members)} publications themselves a number; refer to them as "Publication 1" or by first author.

Style: short, clear sentences (about 20-25 words at most), one main point per sentence, sparing \
statistics (main results with effect size and 95% CI).

{blocks}"""

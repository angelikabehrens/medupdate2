"""Prompt-Texte aus MyLuo – wortgleich übernommen, nur als Funktionen gekapselt.

Die Inhalte (Leitfrage, Grundregeln, Zitierpflicht, Kürzungsregeln des Skills
"studien-wuerdigung", State of the Art, Stilanpassung) sind die in MyLuo erarbeiteten und
erprobten Fassungen. Änderungen an der inhaltlichen Logik bitte hier vornehmen."""

# fmt: off


def translate_abstract_prompt(text: str) -> str:
    return (
    "Übersetze den folgenden medizinisch-wissenschaftlichen Abstract "
    "präzise und fachsprachlich korrekt ins Deutsche. Gib NUR die "
    "Übersetzung zurück, ohne Einleitung, Anführungszeichen oder Kommentar.\n\n"
    "Überarbeite/übersetze den Text zudem in einem präzisen, sachlich-akademischen und für "
    "ein wissenschaftliches Manuskript geeigneten Stil.\n\n"
    "1. Grundprinzip: Vermeidung der Ich-/Wir-Perspektive\n\n"
    "Vermeide konsequent die Ich- und Wir-Form sowie personalisierte Formulierungen wie: "
    "\"ich habe …\", \"wir haben …\", \"wir untersuchten …\", \"wir schlossen … ein\", "
    "\"wir analysierten …\", \"wir fanden …\", \"unsere Ergebnisse …\", \"we included …\", "
    "\"we investigated …\", \"we found …\", \"our results …\". Dies gilt unabhängig von der "
    "Sprache des Ausgangs- oder Zieltextes. Auch wenn ein englischer Originaltext die "
    "Wir-Form verwendet, soll diese nicht automatisch übernommen werden. Die wissenschaftliche "
    "Aussage soll stattdessen in einer unpersönlichen, sachlichen oder geeigneten "
    "autorengruppenbezogenen Form wiedergegeben werden.\n\n"
    "Beispiel: \"We included 3,000 patients.\" → \"A total of 3,000 patients were included.\" "
    "(nicht: \"The research group included 3,000 patients.\")\n"
    "Ebenso: \"We found a significant association between X and Y.\" → \"A significant "
    "association between X and Y was observed.\" oder \"The study identified a significant "
    "association between X and Y.\"\n\n"
    "2. Nicht jedes \"Wir\" durch \"die Arbeitsgruppe\" ersetzen\n\n"
    "Ersetze die Wir-Form NICHT schematisch durch \"die Arbeitsgruppe\"/\"the research group\". "
    "Diese Bezeichnung nur verwenden, wenn tatsächlich die konkreten Handlungen der "
    "Forschenden im Vordergrund stehen und stilistisch sinnvoll sind. In vielen Fällen sind "
    "andere Konstruktionen natürlicher und präziser, je nach Kontext:\n"
    "- Bezug auf die Studie: \"die vorliegende Studie …\", \"the present study …\", \"the study "
    "investigated/included …\"\n"
    "- Bezug auf Daten/Analysen: \"die Daten wurden analysiert …\", \"the data were analyzed …\", "
    "\"the analysis demonstrated …\"\n"
    "- Unpersönliche Formulierungen: \"es zeigte sich …\", \"it was found that …\", \"the results "
    "demonstrated …\"\n"
    "- Bezug auf Ergebnisse: \"die Ergebnisse dieser Studie zeigen …\", \"the findings of the "
    "present study indicate …\"\n"
    "- Bezug auf die Forschenden (sparsam, nur bei konkreter Handlung): \"die Arbeitsgruppe "
    "führte … durch\", \"the research group developed …\"\n\n"
    "3. Natürlichkeit und stilistische Variation\n\n"
    "Ziel ist NICHT eine möglichst monotone Aneinanderreihung von Passivkonstruktionen oder "
    "die wiederholte Verwendung von \"die Arbeitsgruppe\"/\"the research group\". Der Text soll "
    "so klingen, wie ihn eine erfahrene wissenschaftliche Autorin/ein erfahrener Autor in "
    "einem internationalen Fachjournal schreiben würde – variiere zwischen aktivischer "
    "Beschreibung der Studie, Passivkonstruktionen, unpersönlichen Formulierungen und Bezug "
    "auf Studie/Analyse/Ergebnisse. Die Vermeidung der Wir-Form darf nicht zu einem "
    "stilistisch schlechteren Text führen – Natürlichkeit, Präzision und Lesbarkeit haben "
    "Vorrang vor dieser schematischen Regel.\n\n"
    "4. Inhaltliche Treue (hat Vorrang vor allem oben Genannten)\n\n"
    "Die ursprüngliche wissenschaftliche Aussage muss vollständig erhalten bleiben: keine "
    "neuen Informationen hinzufügen, keine entfernen, Zahlen/Effektgrößen/Konfidenzintervalle/"
    "p-Werte unverändert übernehmen, Schlussfolgerungen weder stärker noch schwächer "
    "formulieren als im Original, Fachterminologie und etablierte Abkürzungen beibehalten, "
    "keine Kausalität aus einer Assoziation ableiten, Unsicherheiten und Limitationen des "
    "Originals erhalten.\n\n"
    "Priorität: wissenschaftliche Präzision → inhaltliche Treue → Natürlichkeit und "
    "Lesbarkeit → Vermeidung der Ich-/Wir-Form → stilistische Variation.\n\n"
    "Abstract:\n"
    + text
    )


def appraisal_prompt(entry: dict, sprache_kw: str, fulltext_block: str, background_block: str,
                     max_websuchen: int = 10) -> str:
    sprach_hinweis = (
        "WICHTIG: Schreibe deine GESAMTE Antwort auf Englisch, inklusive aller Abschnittsüberschriften. "
        "Verwende dabei exakt diese Übersetzungen der Überschriften: "
        "'1. Stärken der Studie' -> '1. Strengths of the Study', "
        "'2. Schwächen und Limitationen' -> '2. Weaknesses and Limitations', "
        "'3. Prüfung der Schlussfolgerungen der Autoren' -> '3. Examination of the Authors' Conclusions', "
        "'4. Einordnung in den wissenschaftlichen Gesamtkontext' -> '4. Context within the Current Evidence Base', "
        "'5. Bedeutung für die klinische Praxis' -> '5. Clinical Practice Implications', "
        "'Kommentar' -> 'Comment', 'Fazit für die Praxis' -> 'Conclusion for Practice', "
        "'Literatur' -> 'References'. "
        "Alle inhaltlichen Anweisungen unten bleiben unverändert gültig, nur die Ausgabesprache ändert sich."
        if sprache_kw == "Englisch"
        else "Schreibe deine gesamte Antwort auf Deutsch, wie unten mit den deutschen Überschriften vorgegeben."
    )
    sprach_hinweis += (
        "\n\nWICHTIG – Websuche und Sprache: Auch wenn Suchergebnisse aus Abschnitt 4 auf Englisch "
        "vorliegen (unabhängig von der Zielsprache dieser Antwort), fasse deren Inhalt VOLLSTÄNDIG in "
        "eigenen Worten in der oben festgelegten Zielsprache zusammen. Übernimm NIEMALS einzelne Sätze "
        "oder Satzfragmente wörtlich oder unübersetzt aus einem Suchergebnis in deine Antwort – auch "
        "nicht teilweise oder als Ergänzung zu einer bereits übersetzten Aussage. Die gesamte Antwort "
        "muss durchgehend in einer einzigen Sprache verfasst sein, ohne Sprachwechsel mitten im Text."
    )

    VOLLTEXT_HINWEIS = (
        "Nutze den Volltext vorrangig für die methodische und statistische Bewertung. "
        "Prüfe insbesondere Studiendesign, Population, Endpunkte, Randomisierung bzw. "
        "Auswahlmechanismen, Verblindung, Fallzahlplanung, statistische Analyse, Umgang "
        "mit fehlenden Daten, Follow-up, Protokollabweichungen, Finanzierung, "
        "Interessenkonflikte und vom Abstract nicht erfasste Limitationen. Bei "
        "Abweichungen zwischen Abstract und Volltext ist der Volltext maßgeblich; "
        "benenne die Abweichung."
    )
    volltext_hinweis = VOLLTEXT_HINWEIS if entry.get("full_text") else ""
    prompt = f"""Du bist ein erfahrener klinisch-wissenschaftlicher Experte und verfasst eine wissenschaftlich präzise, methodisch fundierte und kritisch ausgewogene Würdigung einer wissenschaftlichen Publikation für ein medizinisches Fortbildungshandbuch (med update). Die Zielgruppe besteht aus Fachärztinnen und Fachärzten sowie einem entsprechend wissenschaftlich qualifizierten Fachpublikum.

Diese Würdigung ist die ausführliche Arbeitsfassung. Sie wird später für das Handbuch gekürzt. Vollständigkeit und Genauigkeit haben daher Vorrang vor Kürze.

{sprach_hinweis}

Titel: {entry['title']}
Zeitschrift: {entry['journal']} ({entry['pubdate']})
Publikationstyp: {', '.join(entry['pub_types'])}
Abstract:
{entry['abstract']}
{fulltext_block}
{background_block}
{volltext_hinweis}

LEITFRAGE

Die gesamte Würdigung dient der Beantwortung von drei Fragen:
1. Liefert die Studie bisher nicht bekannte Ergebnisse?
2. Sind diese Ergebnisse valide?
3. Sollten sie die klinische Praxis verändern, und wenn ja, für wen?

GRUNDREGELN DER WISSENSCHAFTLICHEN BEWERTUNG

1. Identifiziere zunächst die konkrete wissenschaftliche Fragestellung, das Studiendesign und die primäre Zielsetzung der Studie.
2. Beurteile die methodische Qualität anschließend anhand der für das jeweilige Studiendesign relevanten Kriterien. Wende Kriterien nicht schematisch an, wenn sie für das konkrete Design nicht sinnvoll oder nicht anwendbar sind.
3. Unterscheide strikt zwischen:
a) Angaben und Ergebnissen aus der vorliegenden Publikation,
b) Aussagen aus externer wissenschaftlicher Literatur,
c) deiner eigenen wissenschaftlichen Einordnung.
4. Eine Information, die im Manuskript nicht berichtet wird, darf nicht als nicht durchgeführt oder nicht vorhanden interpretiert werden. Verwende in solchen Fällen Formulierungen wie „nicht berichtet" oder „Auf Grundlage der vorliegenden Informationen ist dies nicht eindeutig beurteilbar."
5. Erfinde keine Daten, Methoden, Ergebnisse, Quellen oder Studienmerkmale.
6. Bewerte statistische Ergebnisse niemals allein anhand von p-Werten. Berücksichtige insbesondere Effektgröße, Konfidenzintervalle, Präzision der Schätzung, absolute und relative Effekte, klinische Relevanz und – sofern relevant – die vorher definierte klinisch relevante Effektgröße.
7. Eine fehlende statistische Signifikanz darf nicht automatisch als fehlender Effekt und nicht automatisch als Hinweis auf eine Unterpowerung interpretiert werden. Beurteile anhand von Effektgröße und Konfidenzintervall, ob klinisch relevante Effekte weiterhin plausibel sind. Formuliere in der eigenen Bewertung „keine signifikante Verbesserung" bzw. „nicht signifikant höher", nicht „verbesserte nicht".
8. Unterscheide zwischen statistischer Signifikanz, klinischer Relevanz und wissenschaftlicher Bedeutung.
9. Unterscheide zwischen präspezifizierten Analysen und post-hoc bzw. explorativen Analysen.
10. Beurteile die Schlussfolgerungen der Autoren unabhängig von der Formulierung der Autoren selbst. Prüfe ausdrücklich, ob die Schlussfolgerungen durch Design, Daten und statistische Analyse tatsächlich gedeckt sind.
11. Übernimm keine wertenden Aussagen der Autoren ungeprüft.
12. Verwende keine pauschalen Qualitätsurteile ohne konkrete methodische Begründung.
13. Wenn Unsicherheit besteht, benenne diese ausdrücklich.
14. Gib die Schlussfolgerung der Autoren inhaltlich vollständig und mit deren Hedging wieder. Verschärfe oder streiche keine Einschränkungen („möglicherweise", „könnte", „kein Hinweis auf einen Unterschied" usw.) und übernimm keine Teilaussage verkürzt.
15. Stelle keine Kausalität her, die in den Daten nicht angelegt ist. Aus „im Zusammenhang mit" wird nicht „bedingt durch".
16. Nenne bei Zahlen immer den Bezug: Studienarm, Analysepopulation (ITT, PP) und Messzeitpunkt bzw. Messstand. Runde oder verändere keine Zahlen aus der Publikation.
17. Nenne Arzneimittel ausschließlich mit dem Wirkstoff bzw. generischen Namen, nie mit dem Handelsnamen. Nenne Medizinprodukte, Geräte und Software mit ihrem Produktnamen und ergänze bei der ersten Nennung knapp das Funktionsprinzip.
18. Die Abkürzung „KI" ist mehrdeutig. Schreibe bei künstlicher Intelligenz die Erstnennung aus („künstliche Intelligenz (KI)"). Schreibe „Konfidenzintervall" im Fließtext aus; in Zahlenklammern ist „95%-KI" zulässig.
19. Prüfe bei jeder berichteten Subgruppenanalyse ausdrücklich, ob die Publikation innerhalb dieser Subgruppe noch eine weiter aufgeschlüsselte, verschachtelte Analyse enthält (z.B. die Untergruppe derjenigen, bei denen die initial geprüfte Strategie tatsächlich versagte) – eine solche tiefer verschachtelte Analyse liefert oft den klinisch aussagekräftigsten Einzelwert und darf nicht übersehen werden, nur weil bereits die übergeordnete Subgruppe berichtet wurde.

ABLAUF

Führe zuerst die Literaturrecherche für Abschnitt 4 mit der Websuche durch. Gib vor und zwischen den Suchen keinen Text aus. Beginne die Ausgabe erst nach Abschluss der Recherche, und zwar unmittelbar mit der Überschrift „1. Stärken der Studie".

Erstelle die kritische Würdigung mit genau den folgenden Abschnitten:

1. Stärken der Studie

Bewerte die für das konkrete Studiendesign relevanten methodischen Stärken.

Berücksichtige – soweit zutreffend:

Studiendesign:
Eignung des Designs für die Fragestellung, prospektiv vs. retrospektiv, randomisiert vs. nicht randomisiert, Kontrollgruppe, Vergleichbarkeit der Gruppen, Verblindung, Allocation Concealment und zeitliche Beziehung zwischen Exposition/Intervention und Outcome.

Studienpopulation:
Fallzahl, Population, Ein- und Ausschlusskriterien, Rekrutierungsweg, Selektionsmechanismen, Repräsentativität, Baseline-Charakteristika und Übertragbarkeit.

Intervention bzw. Exposition:
Definition und Messung der Intervention/Exposition, Dosierung bzw. Intensität, Adhärenz, Vergleichbarkeit, Co-Interventionen und relevante Abweichungen.

Endpunkte:
Primärer und sekundäre Endpunkte, klinische Relevanz, Patientenzentrierung, Validität und Reliabilität der Messung, Zuverlässigkeit des Referenzstandards, objektive vs. subjektive Endpunkte sowie Surrogatendpunkte.

Statistik:
Geeignetheit der statistischen Methoden, Fallzahlplanung, zugrunde liegende Annahmen der Fallzahlberechnung, Power, Effektmaße, Konfidenzintervalle, Adjustierung, Umgang mit Confounding, Missing Data, ITT- bzw. andere Analysepopulationen, multiple Testungen, Interaktionen und Subgruppenanalysen.

Durchführung:
Follow-up, Vollständigkeit der Datenerhebung, Protokolltreue, Transparenz der Methodik und Robustheit der Datenerhebung.

Nenne nur solche Stärken, die anhand der Publikation tatsächlich nachvollziehbar sind.

2. Schwächen und Limitationen

Prüfe systematisch alle relevanten Limitationen der Studie.

Berücksichtige – abhängig vom Studiendesign – insbesondere:

Design-Limitationen, fehlende oder ungeeignete Kontrollgruppen, fehlende Randomisierung, fehlende oder unzureichende Verblindung (mit Angabe, welche Endpunkte davon betroffen sind), unzureichendes Allocation Concealment, Selektionsbias, Confounding, Residual Confounding, Informationsbias, Recall Bias, Observer Bias, Performance Bias, Detection Bias, Attrition Bias (differenzielle Austritte mit Mechanismus), Immortal-Time-Bias, Time-Lag-Bias, Misclassification, Regression to the Mean, kurze oder unvollständige Nachbeobachtung, kleine Fallzahl, unzureichende Präzision, unzureichende Power (angenommene vs. beobachtete Ereignisraten und Effektgrößen – falls die ursprüngliche Fallzahlplanung auf einer inzwischen als überholt erkannten Risiko- oder Ereignisratenannahme beruhte, beziffere nach Möglichkeit grob, um welchen Faktor die tatsächlich benötigte Fallzahl von der ursprünglich geplanten abweicht, statt die Diskrepanz nur zu benennen), ungeeignete Endpunkte, Surrogatendpunkte, Endpunktänderungen während der Studie, fehlender Äquivalenz- oder Nicht-Unterlegenheitstest, obwohl die Schlussfolgerung Gleichwertigkeit nahelegt, multiple Vergleiche, post-hoc Analysen, explorative Subgruppenanalysen, fehlende Adjustierung relevanter Confounder, fehlende Daten, ungeeignete Imputationsverfahren, Änderungen der Analysepopulation, Ausschlüsse aus der PP-Analyse, Protokollabweichungen, Crossovers, Kontamination zwischen Studienarmen, Adhärenz, unterschiedliche Co-Interventionen, ungewöhnlich hohe Qualität der Kontrollgruppe (begrenzt den messbaren Zusatznutzen), fehlende Biomarker- oder Subgruppen-Stratifizierung, Heterogenität der eingeschlossenen Population (z. B. biologisch oder prognostisch unterschiedliche Subgruppen wie Tumor-Subtypen, -Stadien oder Risikoprofile, die unterschiedlich auf die Intervention ansprechen könnten – prüfe dies kritisch auch dann, wenn die Autoren die gemeinsame Einbeziehung selbst begründen, statt diese Begründung ungeprüft zu übernehmen), Lücke zwischen klinischem bzw. bildgebendem Ansprechen und pathologisch gesichertem Ansprechen (insbesondere wenn eine folgenreiche Therapieentscheidung wie Verzicht auf Operation, Active Surveillance oder Watch-and-Wait auf einer nicht-pathologischen Bewertung beruht – benenne diese Lücke explizit als strukturelle Einschränkung, auch wenn die zugrunde liegende diagnostische Genauigkeit in einer separaten Validierungsstudie geprüft wurde), Unterschiede zwischen Studienpopulation und klinischer Routinepopulation, auffällig hohe oder niedrige berichtete Häufigkeiten/Ereignisraten im Vergleich zur allgemein bekannten Größenordnung für diese Erkrankung oder Population (eine solche Abweichung weist häufig auf eine stark vorselektierte Population, eine abweichende Falldefinition oder einen anderen Nenner hin und sollte explizit benannt werden, statt die berichtete Zahl unkommentiert zu übernehmen), Repräsentativität von Zentren und Untersuchern, zeitliche bzw. historische Effekte, Veralterung von Intervention oder Standard während der Studienlaufzeit, Single-Center-Design, Interessenkonflikte, Entwicklerrolle, Herstellerbeteiligung, Finanzierung, Inkonsistenzen zwischen berichteten Daten sowie mögliche Selektions- oder Publikationsverzerrungen.

Nenne bei jeder relevanten Limitation die Richtung des möglichen Bias: zur Null hin (Effekt eher unterschätzt) oder zugunsten eines bestimmten Arms (Effekt eher überschätzt), oder gib an, dass die Richtung nicht beurteilbar ist.

Nenne ausdrücklich auch alle Limitationen, die die Autoren selbst im Manuskript diskutieren. Lasse keine im Manuskript explizit benannte relevante Limitation aus.

Unterscheide anschließend zwischen:

a) geringfügigen Limitationen,
b) relevanten Limitationen,
c) Limitationen, die die zentrale Interpretation oder Schlussfolgerung substanziell infrage stellen können.

Begründe jede Einstufung anhand konkreter Merkmale der Studie.

Beurteile außerdem, ob die genannten Limitationen tatsächlich die Aussagekraft der Studie beeinflussen oder lediglich formale Einschränkungen darstellen.

Schließe den Abschnitt mit einem Gesamturteil zur Validität: Was schließt das Konfidenzintervall aus, was nicht? Wie verhalten sich absolute und relative Effekte?

3. Prüfung der Schlussfolgerungen der Autoren

Gib zunächst die Schlussfolgerung der Autoren vollständig und mit deren Hedging wieder.

Prüfe dann systematisch die logische Kette:

Fragestellung → Studiendesign → erhobene Daten → statistische Analyse → Ergebnisse → Interpretation → Schlussfolgerung.

Beantworte insbesondere:

Sind die Schlussfolgerungen durch die Daten gedeckt?
Gehen die Autoren über die tatsächlich erhobenen Daten hinaus?
Wird aus einer Assoziation eine Kausalität abgeleitet?
Wird statistische Signifikanz mit klinischer Relevanz gleichgesetzt?
Ist die beobachtete Effektgröße klinisch relevant?
Sind die Konfidenzintervalle ausreichend präzise?
Werden Unsicherheiten angemessen berücksichtigt?
Werden sekundäre oder post-hoc Ergebnisse möglicherweise überproportional hervorgehoben?
Werden Surrogatparameter wie harte klinische Endpunkte interpretiert?
Sind Aussagen zur Generalisierbarkeit durch die Studienpopulation gedeckt?

Formuliere anschließend eine eigene, nüchterne Zusammenfassung dessen, was anhand der Daten tatsächlich geschlussfolgert werden kann.

4. Einordnung in den wissenschaftlichen Gesamtkontext

Nutze die Websuche, um die Studie in den bestehenden wissenschaftlichen Kenntnisstand einzuordnen. Dir stehen bis zu {max_websuchen} Suchanfragen zur Verfügung. Nutze sie gezielt und mit unterschiedlichen Suchbegriffen, um die Evidenzlage möglichst vollständig abzubilden.

Suche vorrangig nach methodisch relevanten und möglichst aktuellen:

* systematischen Reviews und Metaanalysen,
* randomisierten kontrollierten Studien,
* großen prospektiven oder relevanten Beobachtungsstudien,
* Leitlinien (insbesondere AWMF-/S3-Leitlinien sowie europäische und internationale Fachgesellschaften),
* Konsensuspapieren,
* gegebenenfalls grundlegenden früheren Studien,
* gegebenenfalls begleitenden Editorials zur vorliegenden Studie.

Priorisiere Quellen, die die konkrete Fragestellung, Population und Intervention bzw. Exposition möglichst direkt betreffen.

Prüfe:

1. Was war vor der vorliegenden Studie bereits bekannt?
2. Ist die untersuchte Fragestellung bereits umfassend untersucht?
3. Welche zentrale Aussage der Studie ist tatsächlich neu?
4. Welche Aussagen bestätigen lediglich bereits bekannte Erkenntnisse?
5. Erweitert die Studie bestehende Erkenntnisse hinsichtlich Population, Intervention, Endpunkt, Dauer oder Methodik?
6. Widerspricht sie früheren Ergebnissen?
7. Falls ein Widerspruch besteht: Welche methodischen oder populationsbezogenen Unterschiede könnten ihn erklären?
8. Gibt es größere oder methodisch robustere Studien mit abweichenden Ergebnissen?
9. Wie verändert die vorliegende Studie die bestehende Evidenzlage tatsächlich?
10. Ist der behauptete Erkenntnisgewinn durch die Qualität und Präzision der Studie gerechtfertigt?

Trenne ausdrücklich zwischen „neu", „bestätigend", „hypothesengenerierend" und „nicht ausreichend belegt", sofern diese Einordnung anhand der Evidenz begründbar ist.

5. Bedeutung für die klinische Praxis

Beurteile die praktische Bedeutung der Ergebnisse.

Prüfe:

* Für welche Patientengruppe sind die Ergebnisse relevant?
* Sind die Studienteilnehmer mit typischen Patienten im klinischen Alltag in Deutschland vergleichbar (z. B. Prävalenz, Versorgungsstruktur)?
* Ist die Intervention bzw. Exposition im klinischen Alltag realistisch?
* Liegt die in der Studie untersuchte Dosierung, Behandlungsdauer, Kombination oder Anwendungsform \
außerhalb der zugelassenen Indikation (Off-Label-Use)? Falls ja, benenne dies ausdrücklich und getrennt \
von der allgemeinen Sicherheits- und Evidenzbewertung, da Off-Label-Use eigene Konsequenzen hat \
(Aufklärungspflicht gegenüber Patientinnen und Patienten, Haftungsfragen, Erstattungsfähigkeit).
* Ist der beobachtete Effekt groß genug, um eine klinische Entscheidung zu beeinflussen?
* Welche Risiken oder Nebenwirkungen sind relevant? Berichte dabei, sofern in der Publikation \
angegeben, IMMER auch die Gesamtrate schwerer unerwünschter Ereignisse bzw. Ereignisse vom Grad ≥3 \
im Gruppenvergleich – auch wenn eine einzelne, naheliegendere Nebenwirkungskategorie (z. B. die \
häufigste oder am stärksten erwartbare) bereits berichtet wurde. Eine Gesamtrate von null schwerer \
Ereignisse darf nicht unterstellt werden, wenn die Publikation tatsächlich eine Grad-≥3-Rate größer \
null angibt.
* Gibt es relevante Unsicherheiten hinsichtlich Nutzen und Schaden?
* Gibt es praktische, organisatorische oder ökonomische Einschränkungen?
* Gibt es eine konkrete, unmittelbar umsetzbare Alternativmaßnahme (z. B. Substitution statt vollständiger \
Elimination, Dosisreduktion, ein anderes Produkt oder Vorgehen), die sich direkt aus den Studiendaten oder \
dem in Abschnitt 4 recherchierten Kontext ableiten lässt – nicht nur eine allgemeine Grundhaltung wie \
„Reduktion" oder „psychologische Unterstützung"? Nenne sie konkret, wenn die Evidenz sie trägt; erfinde \
keine, wenn sie nicht belegt ist.
* Wie verhalten sich die Ergebnisse zu aktuellen Leitlinienempfehlungen?
* Würden die Ergebnisse das klinische Vorgehen tatsächlich verändern oder liefern sie zunächst nur zusätzliche Evidenz?

Unterscheide ausdrücklich zwischen

„Die Studie zeigt …"

und

„Daraus könnte sich für die klinische Praxis ergeben …"

Leite keine Therapieempfehlung ab, die über das hinausgeht, was die Studie und die verfügbare Evidenz tragen.

Kommentar

Formuliere einen kurzen, pointierten Kommentar aus der Perspektive einer klinisch tätigen wissenschaftlichen Referentin bzw. eines Referenten. Er dient als Vorschlag, den die Referentin oder der Referent anschließend selbst anpasst.

Der Kommentar soll 2–3 Sätze umfassen und die Studie auf die klinische Erfahrungsebene übertragen.

Er soll:

* konkret,
* wissenschaftlich fair,
* kritisch,
* aber nicht polemisch sein.

Die Einschätzung muss anhand mindestens eines konkreten Studienmerkmals begründet werden.

Kennzeichne die Aussage sprachlich als klinische bzw. wissenschaftliche Einschätzung und nicht als objektiv bewiesene Tatsache.

Vermeide pauschale Urteile wie „gute Studie", „schlechte Studie", „überzeugend" oder „nicht überzeugend", sofern diese nicht durch konkrete methodische Merkmale erläutert werden.

Fazit für die Praxis

Formuliere 2–3 Sätze Fließtext. Das Fazit enthält die dezidierte Einschätzung aus Expertensicht und, soweit die Evidenz es trägt, eine konkrete Handlungsaussage.

Es beantwortet die Leitfrage ausdrücklich:

1. Ist das Ergebnis neu?
2. Ist es valide?
3. Ändert es die Praxis, und für wen? Wähle erkennbar eine dieser Antworten: ja / für eine definierte Subgruppe / noch nicht / nein.

Die Praxisaussage muss sich aus Neuheit und Validität ergeben. Neue, aber eingeschränkt valide Ergebnisse führen zu „noch nicht" oder „für eine Subgruppe", nicht zu „ja". Vermeide pauschale oder überzogene Aussagen.

Literatur

Nur wenn in Abschnitt 4 oder 5 externe Quellen oder bereitgestellte Hintergrunddokumente zitiert wurden.

WICHTIG – ZITIERPFLICHT (gilt für Abschnitt 4 und 5):

Jede Aussage, die sich auf eine externe Quelle aus der Websuche oder auf ein bereitgestelltes Hintergrunddokument stützt, muss unmittelbar mit einem nummerierten Klammerverweis versehen werden.

Format:
Aussage (1).
Aussage mit mehreren Quellen (2,3).

Verwende ausschließlich runde Klammern. Verwende niemals eckige Klammern für Literaturverweise.

Verwende im Fließtext keine Autorennamen oder Jahreszahlen als Literaturzitat. Verwende ausschließlich die nummerierten Klammerverweise.

Setze einen Literaturverweis nur dann, wenn die jeweilige Quelle die konkrete Aussage tatsächlich unterstützt.

Wenn mehrere unmittelbar aufeinanderfolgende Sätze eindeutig dieselbe Quelle zusammenfassen, genügt ein Verweis am Ende des zusammenhängenden Absatzes.

Jede im Literaturverzeichnis aufgeführte Quelle muss mindestens einmal in Abschnitt 4 oder 5 zitiert werden. Umgekehrt darf keine Quelle im Literaturverzeichnis erscheinen, die dort nicht zitiert wurde.

Verwende ausschließlich Quellen, die du in dieser Recherche tatsächlich gefunden hast oder die als Hintergrunddokument bereitgestellt wurden. Erfinde niemals DOI, PMID, Autorennamen, Zeitschriftentitel, Band- oder Seitenangaben.

Liste sämtliche zitierten Quellen als fortlaufend nummerierte Liste auf. Die Nummerierung entspricht exakt der Reihenfolge des ersten Auftretens im Text.

Format nach den Autorenhinweisen:

1. Nachname Initialen, Nachname Initialen, Nachname Initialen. Titel. Journal. Jahr;Band:Seiten. PMID oder DOI, sofern verfügbar.

Bei mehr als drei Autoren: die ersten drei Autoren, dann „et al.". Fehlen Band oder Seiten in der gefundenen Quelle, lasse sie weg, statt sie zu ergänzen.

Geht der Autor aus dem ersten Suchtreffer nicht hervor, prüfe gezielt auch die PubMed- bzw. Verlagsseite der Quelle. Ist die Autorenschaft trotz gezielter Suche tatsächlich nicht auffindbar (z.B. bei Leitlinien oder Konsensuspapieren mit institutioneller Autorenschaft), beginne den Eintrag stattdessen direkt mit dem Titel oder nenne die herausgebende Institution/Fachgesellschaft anstelle von Personennamen – schreibe NIEMALS einen Platzhaltersatz wie "Autorenschaft nicht erfasst" in den Eintrag.

Keine Aufzählungszeichen außer der Nummer.

Vor Abschluss der Antwort prüfen:

* Wird jede Nummer des Literaturverzeichnisses mindestens einmal im Text verwendet?
* Gibt es im Literaturverzeichnis eine Nummer, die im Text nicht vorkommt?
* Gibt es einen Klammerverweis im Text, der nicht im Literaturverzeichnis enthalten ist?
* Sind alle bibliografischen Angaben tatsächlich durch die gefundene Quelle belegt?

Wenn keine externen Quellen verwendet wurden, lasse den Abschnitt „Literatur" vollständig weg.

ALLGEMEINE QUALITÄTSKONTROLLE VOR DER AUSGABE

Führe vor der finalen Ausgabe eine interne Konsistenzprüfung durch. Gib diese Prüfung nicht aus.

Prüfe insbesondere:

1. Wurde das Studiendesign korrekt identifiziert?
2. Wurden nur für dieses Design relevante Qualitätskriterien angewendet?
3. Wurde zwischen „nicht berichtet" und „nicht durchgeführt" unterschieden?
4. Wurde die Fallzahlplanung tatsächlich anhand der berichteten Informationen bewertet?
5. Wurde „unterpowered" nicht allein aus einer fehlenden statistischen Signifikanz abgeleitet?
6. Wurden Effektgrößen und Konfidenzintervalle berücksichtigt?
7. Wurde statistische Signifikanz von klinischer Relevanz getrennt?
8. Wurden alle im Manuskript selbst genannten relevanten Limitationen berücksichtigt, jeweils mit Richtung des möglichen Bias?
9. Wurden post-hoc Analysen und Subgruppenanalysen als solche erkannt?
10. Wurde die Schlussfolgerung der Autoren vollständig, mit Hedging und unabhängig von deren eigener Darstellung geprüft?
11. Wurde zwischen Daten, Interpretation und eigener wissenschaftlicher Einordnung unterschieden?
12. Wurde die tatsächliche Neuigkeit der Studie gegenüber dem bisherigen Wissen bestimmt?
13. Wurden externe Aussagen vollständig und korrekt zitiert?
14. Wurden keine Informationen, Daten oder Quellen erfunden, und stimmt jede Zahl mit der Publikation überein?
15. Beantwortet das Fazit für die Praxis alle drei Teile der Leitfrage?
16. Wurden Arzneimittel nur mit Wirkstoffnamen genannt?

FORMATIERUNG

Verwende keine Markdown-Formatierung.

Keine Raute-Zeichen vor Überschriften.
Keine Sternchen.
Keine Trennlinien.
Keine Tabellen.

Verwende die Abschnittsüberschriften exakt wie vorgegeben, jeweils in einer eigenen Zeile und ohne zusätzliche Zeichen:
1. Stärken der Studie
2. Schwächen und Limitationen
3. Prüfung der Schlussfolgerungen der Autoren
4. Einordnung in den wissenschaftlichen Gesamtkontext
5. Bedeutung für die klinische Praxis
Kommentar
Fazit für die Praxis
Literatur

STIL

Schreibe in ganzen, gut verständlichen Sätzen, ohne Telegrammstil.

Verwende beim Beschreiben der Studie keine Ich-/Wir-Perspektive der Originalautoren.

Vermeide Formulierungen wie:
„wir untersuchten …"
„wir schlossen … ein"
„we included …"
„we found …"

Verwende stattdessen natürliche studienbezogene Formulierungen wie:
„Die Studie untersuchte …"
„Es wurden … Patienten eingeschlossen."
„Die Analyse zeigte …"

Verwende „die Arbeitsgruppe" oder vergleichbare Formulierungen nur dann, wenn tatsächlich eine konkrete Handlung der Forschenden beschrieben wird.

Inhaltliche Präzision, wissenschaftliche Genauigkeit und natürliche Lesbarkeit haben Vorrang vor einer schematischen Anwendung dieser Stilregel.

SCHLUSSPRINZIP

Die zentrale Aufgabe besteht nicht darin, möglichst viele Kritikpunkte aufzuzählen, sondern die wissenschaftliche Belastbarkeit der zentralen Aussage der Publikation nachvollziehbar zu bestimmen.

Jede wesentliche Kritik muss deshalb eine erkennbare Verbindung zwischen methodischem Merkmal und möglicher Auswirkung auf die Aussagekraft der Studie herstellen.

Eine Limitation ist nicht allein deshalb relevant, weil sie existiert. Entscheidend ist, ob und in welchem Ausmaß sie die Validität, Präzision, Interpretation, Generalisierbarkeit oder klinische Bedeutung der Ergebnisse beeinflusst."""
    return prompt


def translate_appraisal_prompt(source_text: str, target_lang: str) -> str:
    if target_lang == "Englisch":
        header_map = (
            "'1. Stärken der Studie' -> '1. Strengths of the Study', "
            "'2. Schwächen und Limitationen' -> '2. Weaknesses and Limitations', "
            "'3. Prüfung der Schlussfolgerungen der Autoren' -> '3. Examination of the Authors' Conclusions', "
            "'4. Einordnung in den wissenschaftlichen Gesamtkontext' -> '4. Context within the Current Evidence Base', "
            "'5. Bedeutung für die klinische Praxis' -> '5. Clinical Practice Implications', "
            "'Kommentar' -> 'Comment', 'Fazit für die Praxis' -> 'Conclusion for Practice', "
            "'Literatur' -> 'References'."
        )
        ziel = "Englische"
    else:
        header_map = (
            "'1. Strengths of the Study' -> '1. Stärken der Studie', "
            "'2. Weaknesses and Limitations' -> '2. Schwächen und Limitationen', "
            "'3. Examination of the Authors' Conclusions' -> '3. Prüfung der Schlussfolgerungen der Autoren', "
            "'4. Context within the Current Evidence Base' -> '4. Einordnung in den wissenschaftlichen Gesamtkontext', "
            "'5. Clinical Practice Implications' -> '5. Bedeutung für die klinische Praxis', "
            "'Comment' -> 'Kommentar', 'Conclusion for Practice' -> 'Fazit für die Praxis', "
            "'References' -> 'Literatur'."
        )
        ziel = "Deutsche"

    prompt = f"""Übersetze den folgenden strukturierten Text vollständig ins {ziel}. Übersetze dabei die \
Abschnittsüberschriften exakt wie folgt: {header_map}

WICHTIG: Lasse alle Klammerverweise (z.B. "(1)", "(2,3)") exakt unverändert an derselben Stelle im jeweiligen \
Satz stehen – nicht übersetzen, nicht verschieben, nicht verändern. Übernimm die nummerierte Literaturliste am \
Ende unverändert in Nummerierung, Reihenfolge und Inhalt (Autor, Publikationstitel, Journal, Jahr, PMID/DOI \
bleiben exakt wie im Original, auch wenn der Publikationstitel dadurch nicht in der Zielsprache ist). \
Übersetze ausschließlich den erklärenden Fließtext. Gib NUR den übersetzten Text zurück, ohne Einleitung, \
Anführungszeichen oder Kommentar davor oder danach.

Text:
{source_text}"""
    return prompt


LAENGEN_STUFEN = {
    "Sehr kurz": 160,
    "Kurz": 275,
    "Mittel": 500,
    "Ausführlich": 750,
}


SKILL_LAENGEN_STUFEN = {
    "Ausführlich": None,
    "Kurznachricht": 75,
    "½ Seite (Kurz)": 160,
    "1 Seite": 370,
    "1,5 Seiten (Lang)": 570,
    "2 Seiten": 700,
}
SKILL_LAENGEN_BESCHREIBUNGEN = {
    "Ausführlich": "Die vollständige, ungekürzte kritische Würdigung bleibt unverändert.",
    "Kurznachricht": "≈60–90 Wörter · höchstens 3 bündige Sätze als reiner Fließtext: Satz 1 nennt "
                     "Studienziel, Studiendesign und Zahl der eingeschlossenen Patienten; danach "
                     "ausschließlich die Hauptaussage der Studie (wird gegen Fazit, Primärendpunkt und "
                     "Validitätsurteil geprüft). Keine Literaturverweise.",
    "½ Seite (Kurz)": "≈160 Wörter (bis 220 erlaubt) · NUR Fließtext, ein bis zwei Absätze. Kein "
                      "Abstract-Block, kein Kommentar, kein Fazit-Absatz. Der letzte Satz ist "
                      "die Kernaussage der Publikation.",
    "1 Seite": "≈370 Wörter · Abstract-Block (Studie/Ergebnisse/Schlussfolgerung der Autoren), "
               "dann Würdigung als Fließtext, dann eigener Absatz „Fazit für die Praxis“. Kein "
               "persönlicher Kommentar.",
    "1,5 Seiten (Lang)": "≈570 Wörter · wie „1 Seite“, zusätzlich ein persönlicher, klinisch "
                        "begründeter „Kommentar“-Absatz vor dem Fazit. Berücksichtigt alle "
                        "Inhalte.",
    "2 Seiten": "≈700 Wörter für Würdigung + Kommentar (Fazit kommt unverändert hinzu) · "
                "vollständiger Original-Abstract (unverändert, keine KI-Zusammenfassung), dann "
                "Würdigung als Fließtext mit den zwei Schwerpunkten Studienqualität und Relevanz "
                "der Studie, dann „Kommentar“ (2-3 Sätze, wie im Original), dann „Fazit für die "
                "Praxis“ vollständig und wortgleich aus der ausführlichen Fassung übernommen. "
                "Erste Kalibrierung – Zielwort noch nicht an echten Vorlagen-Seiten geprüft.",
}


def kurznachricht_prompt(full_text: str, sprache_kw: str, abstract_text: str = "") -> str:
    ziel_sprache = "englischer" if sprache_kw == "Englisch" else "deutscher"
    abstract_block = (
        f"\n\nAbstract der Studie (Originalangaben, z. B. für Design und Patientenzahl):\n{abstract_text}"
        if abstract_text.strip() else ""
    )
    prompt = f"""Du bist wissenschaftlicher Redakteur für eine ärztliche Fortbildung. Schreibe aus der \
folgenden kritischen Würdigung eine KURZNACHRICHT auf {ziel_sprache} Sprache.

AUFBAU – ausschließlich Fließtext in EINEM Absatz, höchstens 3 Sätze:
- Satz 1: Studienziel und Studiendesign in einem Satz. Die Anzahl der eingeschlossenen Patientinnen und \
Patienten (bzw. Teilnehmenden) muss darin genannt werden.
- Danach (Satz 2, höchstens Satz 3): AUSSCHLIESSLICH die Hauptaussage der Studie. Nichts sonst – keine \
Nebenbefunde, keine Methodendetails, keine Einordnung in andere Literatur, keine Empfehlungen.

PRÜFUNG DER HAUPTAUSSAGE (vor dem Schreiben, NICHT mit ausgeben): Die Hauptaussage entspricht häufig dem \
Fazit der Würdigung bzw. der Schlussfolgerung der Autoren. Prüfe aber, ob das hier der Fall ist: Vergleiche \
mit dem Ergebnis zum primären Endpunkt und mit dem Validitätsurteil der Würdigung. Geht Fazit oder \
Autorenschlussfolgerung über das hinaus, was die Daten tragen (z. B. nicht signifikantes Ergebnis, \
relevante Verzerrung, explorative Analyse), dann formuliere die Hauptaussage so, wie die Daten sie tragen \
– nicht so, wie sie zugespitzt wurde. Stimmen Fazit und Datenlage überein, gib die Aussage des Fazits wieder.

REGELN:
- Nur Fließtext: keine Überschrift, keine Labels wie "Ziel:" oder "Fazit:", keine Aufzählung, keine \
Klammerverweise oder Literaturziffern, keine Erläuterung des Vorgehens.
- Höchstens 3 Sätze. Jeder Satz bündig, also ohne verschachtelte Nebensatzketten, und trotz des dichten \
Inhalts gut verständlich. Ganze Sätze, kein Telegrammstil.
- Zahlen, Effektgrößen, Konfidenzintervalle und p-Werte nur unverändert aus dem Text übernehmen; nichts \
hinzufügen, nichts runden, nichts erfinden. Ist die Patientenzahl im Text nicht genannt, schreibe knapp, \
dass sie nicht angegeben ist – rate nicht.
- Hedging weder streichen noch verschärfen ("kein Hinweis auf" wird nicht zu "kein"; "könnte" nicht zum \
Indikativ). Nicht-Signifikanz nicht als Wirkung darstellen. Kausalität nur, wenn im Original so belegt. \
Bei relativen Befunden den Vergleichsarm nennen.
- Keine Ich-/Wir-Form.
- Ausgabe: nur der fertige Text, sonst nichts.

Kritische Würdigung:
{full_text}{abstract_block}"""
    return prompt


def skill_shorten_prompt(full_text: str, tier: str, sprache_kw: str) -> str:
    target_words = SKILL_LAENGEN_STUFEN[tier]
    ziel_sprache = "englischer" if sprache_kw == "Englisch" else "deutscher"
    fazit_label = "Fazit für die Praxis" if sprache_kw != "Englisch" else "Conclusion for Practice"
    kommentar_label = "Kommentar" if sprache_kw != "Englisch" else "Comment"

    if tier == "½ Seite (Kurz)":
        aufbau = f"""Aufbau: NUR Fließtext, ein bis zwei Absätze. KEINE Zwischenüberschriften, KEIN \
Abstract-Block, KEIN Kommentar, KEIN eigener Fazit-Absatz, KEINE Labels wie "Kernaussage:".

Richtwerte für die Gewichtung:
- Design, Setting, Fallzahl, Intervention/Kontrolle, primärer Endpunkt: ca. 25%
- Hauptergebnis mit Effektgröße, 95%-Konfidenzintervall und p-Wert; ggf. ein zentrales sekundäres \
Ergebnis: ca. 30%
- Wichtigste Validitätseinschränkungen mit Bias-Richtung und knappes Validitätsurteil: ca. 25%
- Neuheit/Einordnung in einem Halbsatz: ca. 5-10%
- Letzter Satz (ohne Label): die Kernaussage der Publikation – die Hauptbotschaft der Autoren MIT \
deren Hedging, nicht die Meinung des Rezensenten: ca. 10-15%"""
    elif tier == "1 Seite":
        aufbau = f"""Aufbau in drei Blöcken:
1. Abstract-Block (ca. 30%): mit FETTEN Labels "Studie:", "Ergebnisse:", "Schlussfolgerung der \
Autoren:" (deren Wortlaut MIT Hedging, alle Teilaussagen der Schlussfolgerung enthalten).
2. Würdigung (ca. 58%): Fließtext OHNE Zwischenüberschriften, in 2-3 Absätzen: erst Neuheit, dann \
Validität (Stärken, Limitationen mit Bias-Richtung, Gesamturteil), dann Einordnung und Bedeutung \
für die Praxis inkl. Übertragbarkeit.
3. "{fazit_label}:" (ca. 12%): eigener, sichtbar abgesetzter Absatz, beginnt mit fettem \
"{fazit_label}:". Beantwortet in 2-3 Sätzen: Ist das Ergebnis neu? Ist es valide? Ändert es die \
Praxis, und für wen (ja / für definierte Subgruppe / noch nicht / nein)?

KEIN persönlicher Kommentar: keine Ich-Aussagen, keine subjektiven Erfahrungsberichte."""
    elif tier == "1,5 Seiten (Lang)":
        aufbau = f"""Aufbau in vier Blöcken:
1. Abstract-Block (ca. 25%): wie bei "1 Seite" (fette Labels "Studie:", "Ergebnisse:", \
"Schlussfolgerung der Autoren:").
2. Würdigung (ca. 52%): Fließtext ohne Zwischenüberschriften, 3-4 Absätze: Neuheit (~8%), Validität \
(~24%), Einordnung mit bisheriger Evidenz/Leitlinien (~10%), Bedeutung für die Praxis (~10%).
3. "{kommentar_label}:" (ca. 12%): persönliche, klinisch begründete Einschätzung, eigener Absatz, \
beginnt mit fettem "{kommentar_label}:".
4. "{fazit_label}:" (ca. 11%): eigener Absatz, beginnt mit fettem "{fazit_label}:", beantwortet wie \
oben beschrieben die Leitfrage."""
    else:  # 2 Seiten
        aufbau = f"""Aufbau in drei Blöcken. KEIN Abstract-Block – der Original-Abstract wird von \
der Anwendung separat und unverändert vorangestellt, nicht von dir erzeugt. Beginne direkt mit der \
Würdigung selbst.
1. Würdigung (Fließtext ohne Zwischenüberschriften, 2-3 Absätze) mit genau zwei Schwerpunkten:
   - Studienqualität: Design, Methodik, wichtigste Stärken und Limitationen mit Bias-Richtung, \
Gesamturteil zur Validität.
   - Relevanz der Studie: Einordnung in den wissenschaftlichen Kontext (bisherige Evidenz/Leitlinien) \
und Bedeutung für die klinische Praxis.
2. "{kommentar_label}:" – eigener, sichtbar abgesetzter Absatz, beginnt mit fettem "{kommentar_label}:". \
Umfang WIE IM ORIGINAL (2-3 Sätze) – NICHT ausbauen, nur bei Bedarf leicht sprachlich straffen.
3. "{fazit_label}:" – eigener, sichtbar abgesetzter Absatz, beginnt mit fettem "{fazit_label}:". \
VOLLSTÄNDIG UND WORTGLEICH aus der Originalfassung übernehmen – NICHT kürzen, NICHT umformulieren, \
auch wenn dadurch der Zielumfang überschritten wird."""

    zielumfang_zeile = (
        f"""Zielumfang: ca. {target_words} Wörter für Würdigung und "{kommentar_label}" zusammen \
(Stufe „{tier}"). Das "{fazit_label}"-Feld zählt NICHT zu diesem Zielumfang, da es unverändert aus der \
Originalfassung übernommen wird. Ohne Studienüberschrift, Themenzeile und Literaturliste."""
        if tier == "2 Seiten"
        else f"""Zielumfang: ca. {target_words} Wörter (Stufe „{tier}"). Zählbasis: alle Wörter vom \
ersten Textabsatz bis einschließlich {fazit_label}, ohne Studienüberschrift, Themenzeile und \
Literaturliste."""
    )

    prompt = f"""Du bist wissenschaftlicher Redakteur und kürzt eine bereits erstellte kritische \
Würdigung einer klinischen Studie nach einem etablierten, kalibrierten Verfahren für eine ärztliche \
Fortbildung. Schreibe auf {ziel_sprache} Sprache.

{zielumfang_zeile}

{aufbau}

Leitfrage (muss in jeder Stufe erkennbar beantwortet werden – implizit im Fließtext bei "½ Seite \
(Kurz)", ausdrücklich im {fazit_label}-Absatz bei "1 Seite", "1,5 Seiten (Lang)" und "2 Seiten" – bei \
"2 Seiten" ergibt sich das automatisch, da dieser Absatz unverändert übernommen wird):
1. Liefert die Studie bisher nicht bekannte Ergebnisse?
2. Sind diese Ergebnisse valide?
3. Sollten sie die klinische Praxis verändern, und wenn ja, für wen?

Priorisierung nach wissenschaftlicher Bedeutung (je kürzer die Stufe, desto strenger einhalten):
1. Primärer Endpunkt mit Effektgröße, Konfidenzintervall und p-Wert
2. Validitätsprobleme mit Einfluss auf die Hauptaussage, samt Gesamturteil
3. Aussage zur Praxis mit Einschränkungen und Übertragbarkeit bzw. Fazit
4. Schlussfolgerung/Kernaussage der Autoren
5. Neuheit und Kontext (Leitlinien, bisherige Evidenz)
6. Sekundäre Endpunkte, Details der Studiendurchführung, Populationsdetails
7. Kommentar

Kürzungsstrategie: Zuerst Redundanzen entfernen (Wiederholungen zwischen Abschnitten, doppelte \
Sätze, Füllformulierungen, sprachliche Ausschmückungen). Ergebnisse konservativer kürzen als \
Einordnung/Diskussion.

GESPERRTE INHALTE – NIEMALS VERÄNDERN: Zahlen, Effektgrößen, Konfidenzintervalle, p-Werte, \
Zeitangaben, Studiendesign, Literaturangaben (auch Klammerverweise). Nicht runden, nicht \
zusammenfassen, nur um Text zu sparen – Weglassen im Rahmen der Priorisierung ist erlaubt, \
Verändern nie.

KEINE BEDEUTUNGSVERSCHIEBUNG – häufigste Fehler, gezielt vermeiden:
- Hedging weder streichen noch verschärfen (z.B. "kein Hinweis auf einen Unterschied" bleibt so, \
wird NICHT zu "kein Unterschied"; "könnte" wird NICHT zum Indikativ; "potenziell" fällt nicht weg).
- Nicht-Signifikanz nicht als "verbesserte nicht" formulieren, sondern "keine signifikante \
Verbesserung"/"nicht signifikant höher".
- Keine Kausalität verdrehen oder neu erfinden ("weil", "daher", "denn", "also" nur wenn im \
Original so begründet).
- Vergleichsarm nicht weglassen, sodass ein relativer Befund absolut klingt; Gruppenzuordnung von \
Zahlen nicht verlieren (z.B. "8 vs. 7" ohne Armbezug vermeiden).
- Mechanismus/Richtung eines Bias nicht weglassen, sodass eine Zahl ohne Deutung dasteht.
- Zuschreibung nicht verschieben: Autoren, Rezensent, Leitlinie klar auseinanderhalten.
- Keine Teilaussage der Autoren-Schlussfolgerung verlieren (z.B. nur einen von zwei Endpunkten \
nennen).
- Explorative Post-hoc-Analysen nicht ohne Hinweis darstellen (Interaktionstest, Multiplizität, \
Vorbehalte der Autoren erhalten).

Literaturverweise in Klammern (z.B. "(1)"), die du übernimmst, müssen weiterhin zu einem Eintrag in \
der Literaturliste passen; entferne den Verweis, wenn du die einzige Stelle kürzt, die ihn benötigt.

WICHTIG: Falls die Originalfassung externe Literaturverweise enthält (Leitlinien, andere Studien, \
Metaanalysen zur Einordnung), bewahre davon mindestens den wissenschaftlich wichtigsten – auch in der \
kürzesten Stufe ("½ Seite") darf die Einordnung nicht komplett ohne jeden externen Verweis auskommen, \
nur weil dieser Abschnitt gekürzt wird. Ein knapper Halbsatz mit Verweis reicht.

Stil: ganze, gut verständliche Sätze mit so wenig Wörtern wie möglich. Kein Telegrammstil, keine \
Aufzählungen im Fließtext. Vermeide die Ich-/Wir-Form der Originalautoren (siehe getrennte \
Stilvorgabe, sofern bereits angewendet).

Ausgabe: Gib ausschließlich den fertigen Text aus, keine Überschrift wie "Zusammenfassung", keine \
Erläuterung des Vorgehens. Priorisiere bei Zielkonflikten immer inhaltliche Korrektheit vor exakter \
Wortzahl.

Bereits erstellte kritische Würdigung:
{full_text}"""
    return prompt


def style_prompt(full_text: str, tier: str, style_samples: list[str], sprache_kw: str) -> str:
    needs_length_change = tier != "Ausführlich"
    needs_style_change = bool(style_samples)
    if not needs_length_change and not needs_style_change:
        return full_text  # nichts zu tun: volle Länge, kein Stilmuster gewünscht

    ziel_sprache = "englischer" if sprache_kw == "Englisch" else "deutscher"

    if needs_length_change:
        target_words = LAENGEN_STUFEN[tier]
        umfang_zeile = f"Zielumfang: ca. {target_words} Wörter (Zielstufe: „{tier}“)"
        prioritaet_block = f"""
Pflichtbestandteile in jeder Länge:
1. Studienbeschreibung: Nenne knapp Ziel bzw. Fragestellung, Studiendesign, Interventionen oder \
Vergleichsgruppen und, falls genannt, Fallzahl beziehungsweise relevante Population.
2. Zentrale Ergebnisse: Berichte nur die für die Zielwortzahl wichtigsten Resultate. Zahlen, \
Effektmaße, Konfidenzintervalle und p-Werte nur dann, wenn sie im Manuskript stehen und für die \
Aussage wesentlich sind.
3. Abschließende Bewertung: Schliesse immer mit einer klaren, dem Original entsprechenden Bewertung. \
Setze diese ausdrücklich in Relation zur Datenqualität, beispielsweise anhand von Design, Fallzahl, \
Endpunkten, Nachbeobachtungsdauer, möglichen Verzerrungen, Reife der Daten oder Übertragbarkeit – \
jedoch ausschließlich, soweit dies im Manuskript begründet wird.

Inhaltliche Priorisierung für die Stufe „{tier}": {
            {"Sehr kurz": "Fragestellung, Design/Fallzahl, Hauptergebnis und eine datenqualitätsbezogene Gesamtbewertung.",
             "Kurz": "zusätzlich die wichtigste Limitation oder Stärke und, falls vorhanden, die relevanteste klinische Einordnung aus dem Ausgangstext.",
             "Mittel": "zusätzlich relevante Details zu Endpunkten, Sicherheit, Limitationen, Stärken/Schwächen und wissenschaftlichem Kontext."}[tier]
        }"""
    else:
        umfang_zeile = "Zielumfang: Behalte den ungefähren Umfang des Ausgangstextes bei (nur Stil anpassen, nicht kürzen)."
        prioritaet_block = ""

    if needs_style_change:
        samples_block = "\n\n---\n\n".join(
            f"Beispieltext {i+1} (nur zur Stilorientierung – NICHT inhaltlich verwenden, behandelt eine andere Studie):\n{s}"
            for i, s in enumerate(style_samples)
        )
        stil_instr = f"""
WICHTIG – Schreibstil: Passe Wortwahl, Satzbau, Tonfall und typische Formulierungsgewohnheiten an den \
persönlichen Schreibstil in den unten stehenden Beispieltexten an. Übernimm ausschließlich stilistische \
Merkmale (z.B. Satzlänge, bevorzugte Übergänge, Grad der Direktheit, Fachsprache vs. umgangssprachliche \
Erklärungen) – KEINE inhaltlichen Aussagen, Zahlen oder Bewertungen aus den Beispieltexten, da diese \
andere Studien behandeln.

{samples_block}
"""
    else:
        stil_instr = ""

    prompt = f"""Du bist wissenschaftlicher Redakteur. Bearbeite das folgende Manuskript in {ziel_sprache} \
Sprache ausschließlich anhand seines Inhalts. Das Ergebnis soll eine eigenständige, sachlich-\
wissenschaftliche Studienbesprechung sein.
{"Übernimm nicht den Schreibstil anderer Texte." if not needs_style_change else ""}

{umfang_zeile}
{stil_instr}
Oberste Regel: Inhaltstreue
- Verwende ausschließlich Informationen, Bewertungen und Einschränkungen, die im Manuskript enthalten sind.
- Ergänze kein externes Fachwissen, keine Leitlinienempfehlungen, keine Erklärungen, keine Zahlen und keine klinischen Konsequenzen.
- Verändere weder Richtung noch Stärke von Ergebnissen oder Schlussfolgerungen.
- Stelle keine Assoziation als Kausalität dar, wenn das Original dies nicht tut.
- Übernimm Unsicherheit, vorsichtige Formulierungen und fehlende statistische Signifikanz korrekt.
- Wenn der Ausgangstext keine Angabe enthält, darf sie nicht ergänzt oder als fehlend problematisiert werden.
{prioritaet_block}

Struktur:
- Verwende einen zusammenhängenden Fließtext, keine Abschnittsüberschriften wie "Kommentar:" oder "Fazit:".
- Die Reihenfolge soll in der Regel lauten: Studienbeschreibung → Ergebnisse → Einordnung/Kritik → abschließende Bewertung.
- Berichte Limitationen oder Stärken nur, wenn sie im Manuskript genannt oder unmittelbar anhand dort explizit beschriebener Studiendaten begründet werden.
- Ist der Ausgangstext selbst wertend, fasse diese Wertung inhaltstreu zusammen; ersetze sie nicht durch eine eigene Bewertung.
- Eine positive Studie darf nicht stärker bewertet werden, als es Qualität und Reife der im Manuskript beschriebenen Daten zulassen. Ebenso dürfen Limitationen nicht überbetont werden.
- Literaturverweise in Klammern (z.B. "(1)"), die du übernimmst, müssen weiterhin zu einem Eintrag in der Literaturliste passen; entferne den Verweis, wenn du die einzige Stelle kürzt, die ihn benötigt.

Ausgabe:
- Gib ausschließlich den fertigen Text aus.
- Keine Überschrift wie „Zusammenfassung", keine Erläuterung des Vorgehens, keine Stichpunkte.
- Priorisiere bei Zielkonflikten immer inhaltliche Korrektheit vor exakter Wortzahl bzw. Stiltreue.

Manuskript:
{full_text}"""
    return prompt


def sota_prompt(thema_text: str, sprache_kw: str) -> str:
    ziel_sprache = "englischer" if sprache_kw == "Englisch" else "deutscher"
    sota_lit_label = "References" if sprache_kw == "Englisch" else "Literatur"

    prompt = f"""Erstelle für das Thema „{thema_text}“ einen Abschnitt mit dem Titel „State of the \
Art“, auf {ziel_sprache} Sprache. Das Thema liegt dir möglicherweise nur als kurzer Suchbegriff vor \
(z.B. „gastric cancer endoscopy“ statt eines ausformulierten Satzes) – interpretiere es sinnvoll als \
Fachthema.

Der Abschnitt soll den aktuellen wissenschaftlichen und klinischen Wissensstand als Ausgangspunkt \
des Manuskripts darstellen. Er soll dem Leser in 3 bis 4 Sätzen (absolutes Maximum: 5 Sätze) vermitteln, wo das Fachgebiet \
aktuell steht, bevor die im weiteren Verlauf des Manuskripts analysierten neuen Publikationen \
betrachtet werden.

BASELINE-EVIDENZ

Erstelle zunächst intern eine Baseline-Evidenzbasis, die den etablierten Wissensstand zum jeweiligen \
Thema abbildet. Nutze die Websuche gezielt, um aktuelle Leitlinien, Konsensuspapiere und \
systematische Reviews zu diesem Thema zu identifizieren. Priorisiere hierfür – sofern verfügbar und \
thematisch geeignet:

1. aktuelle Leitlinien und internationale Empfehlungen,
2. aktuelle Konsensuspapiere und Positionspapiere,
3. systematische Reviews und Metaanalysen,
4. hochwertige epidemiologische bzw. populationsbezogene Daten,
5. etablierte hochwertige Originalarbeiten, wenn für eine bestimmte Aussage keine höherstufige \
Evidenz verfügbar ist.

Falls zu einem sehr spezifischen oder engen Thema keine ausreichende übergeordnete Evidenz auffindbar \
ist, weiche auf den nächsthöheren thematischen Kontext aus, statt den Abschnitt zu erzwingen oder \
Inhalte zu erfinden.

Die Baseline-Evidenz soll den bis zum aktuellen Zeitpunkt etablierten Wissensstand abbilden und \
unabhängig von den Publikationen erstellt werden, die im weiteren Verlauf des Manuskripts analysiert \
und kritisch gewürdigt werden.

Verwende die später analysierten Publikationen nicht, um den State of the Art nachträglich zu \
konstruieren. Sie stellen den Ausgangspunkt für die anschließende wissenschaftliche Diskussion und \
die Weiterentwicklung des bestehenden Wissens dar.

ERSTELLUNG DES STATE OF THE ART

Verfasse anschließend auf Grundlage dieser Baseline-Evidenz einen prägnanten Abschnitt mit \
vorzugsweise 3 bis 4 Sätzen und absolut höchstens 5 Sätzen.

LÄNGE – STRIKT EINZUHALTEN:
* Ziel: 3 bis 4 Sätze. Mehr als 5 Sätze sind nicht zulässig.
* Jeder Satz bündig: höchstens ca. 30 Wörter, keine verschachtelten Nebensatzketten, keine \
Aufzählungen mit Semikolons oder langen Klammerzusätzen.
* Gesamtumfang ca. 70 bis 110 Wörter. Lieber das Wesentliche weglassen als Sätze überladen.
* Die Satzgrenze nicht durch Semikolons oder Gedankenstriche umgehen: Ein Satz bleibt ein Satz.

* Schreibe präzise, sachlich und auf dem Niveau eines wissenschaftlichen Fachpublikums.
* Setze voraus, dass die Leser mit dem Thema vertraut sind.
* Keine Definitionen, keine grundlegende Einführung und keine Lehrbucherklärungen.
* Beantworte primär die Frage: „Wo steht das Fachgebiet aktuell?“
* Berücksichtige – abhängig vom Thema – insbesondere die aktuelle Relevanz, etablierte Verfahren und \
Strategien, deren Stellenwert, relevante Entwicklungen sowie bestehende Limitationen oder \
Evidenzlücken.
* Passe die Gewichtung flexibel an das jeweilige Thema an; nicht jeder Aspekt muss enthalten sein.
* Verwende konkrete Zahlen, Empfehlungen oder Aussagen zur Wirksamkeit nur dann, wenn sie durch \
geeignete Evidenz belastbar gestützt sind.
* Bei widersprüchlicher Evidenz soll die Unsicherheit knapp und neutral dargestellt werden.
* Vermeide Allgemeinplätze, Wiederholungen und Füllsätze.
* Der Text soll wie ein manuskripttauglicher wissenschaftlicher Abschnitt wirken, nicht wie eine \
allgemeine KI-Zusammenfassung.
* Nenne Arzneimittel ausschließlich mit dem Wirkstoff- bzw. generischen Namen, nie mit dem \
Handelsnamen.
* Verwende keine Markdown-Formatierung (keine Sternchen, keine Rauten, keine Trennlinien).

TRENNUNG VON BASELINE UND NEUEN PUBLIKATIONEN

Achte strikt auf die wissenschaftliche Dramaturgie: Baseline-Evidenz → State of the Art → neue \
Publikationen → kritische Würdigung → Synthese des aktuellen Wissens → Manuskript. Der State of the \
Art darf daher keine Erkenntnisse vorwegnehmen, die erst aus den anschließend analysierten \
Publikationen hervorgehen.

LITERATURVERWEISE

Belege die zentralen Aussagen mit nummerierten Klammerverweisen, z.B. "(1)" oder "(2,3)". Der Verweis \
steht IMMER vor dem Satzpunkt ("… etabliert (1)."), niemals dahinter. Belege ausschließlich mit Quellen, \
die du in dieser Recherche tatsächlich gefunden hast und die die Baseline-Evidenz bilden (Leitlinien, \
Konsensuspapiere, systematische Reviews, Metaanalysen, epidemiologische Daten, etablierte Originalarbeiten) \
– niemals mit den später im Manuskript analysierten neuen Publikationen. Verwende nur wenige, tragende \
Quellen (höchstens 5) und fasse mehrere Quellen zu einer Aussage in einem Verweis zusammen, z.B. "(1,2)". \
Schreibe Jahreszahlen nie allein in Klammern.

Liste die zitierten Quellen unter der Überschrift „{sota_lit_label}“ als fortlaufend nummerierte Liste in \
der Reihenfolge des ersten Auftretens im Text auf. Format:

1. Nachname Initialen, Nachname Initialen, Nachname Initialen. Titel. Journal. Jahr;Band:Seiten. PMID oder DOI, sofern verfügbar.

Bei mehr als drei Autoren: die ersten drei, dann „et al.". Fehlen Band oder Seiten in der gefundenen \
Quelle, lasse sie weg, statt sie zu ergänzen. Erfinde niemals DOI, PMID, Autorennamen, Zeitschriftentitel, \
Band- oder Seitenangaben. Geht der Autor aus dem ersten Suchtreffer nicht hervor, prüfe gezielt auch die \
PubMed- bzw. Verlagsseite der Quelle. Ist die Autorenschaft trotz gezielter Suche tatsächlich nicht \
auffindbar (z.B. bei Leitlinien mit institutioneller Autorenschaft), beginne den Eintrag direkt mit dem \
Titel oder nenne die herausgebende Fachgesellschaft anstelle von Personennamen – schreibe NIEMALS einen \
Platzhaltersatz wie "Autorenschaft nicht erfasst".

Jede Nummer der Liste muss mindestens einmal im Text vorkommen, und jeder Klammerverweis im Text muss \
einen Listeneintrag haben. Die Satzbegrenzung gilt nur für den Fließtext, nicht für die Literaturliste. \
Keine Aufzählungszeichen außer der Nummer.

AUSGABE

Gib die Überschrift „State of the Art“, darunter den fertigen Fließtext (vorzugsweise 3 bis 4 Sätze, \
höchstens 5), darunter die Überschrift „{sota_lit_label}“ mit der nummerierten Literaturliste aus. \
Erfinde keine Inhalte."""
    return prompt

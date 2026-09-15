"""One sentence of lore per constellation (#123).

Labels give Bayer designations and proper names, and the narration gives
a paragraph, but a non-astronomer looking at "Aquila" on their photo has
nothing to hold onto. This is one reliable sentence per constellation,
the same every time, keyed by IAU abbreviation: the name, what it is,
and its best-known star where it has one. Original prose from the
standard mythology and naming history (Ptolemy's figures, the Dutch
navigators of the 1590s, Hevelius, Lacaille), so there is no licence to
carry and nothing to attribute.
"""

LORE = {
    "And": "Andromeda is the chained princess of Greek myth, offered to a sea monster and rescued by Perseus; the galaxy that bears her name sits at her hip.",
    "Ant": "Antlia is the air pump, one of the scientific instruments Lacaille placed in the southern sky in the 1750s.",
    "Aps": "Apus is the bird-of-paradise, drawn from travellers' tales of a bird thought to have no feet.",
    "Aql": "Aquila is the eagle that carried Zeus's thunderbolts; Altair is its eye.",
    "Aqr": "Aquarius is the water-bearer, pouring a stream that on the old star maps flows down to the mouth of the southern fish.",
    "Ara": "Ara is the altar on which the gods swore their oath before the war with the Titans.",
    "Ari": "Aries is the ram whose golden fleece Jason sailed to fetch; the Sun stood here at the spring equinox two thousand years ago.",
    "Aur": "Auriga is the charioteer, and Capella, its brightest star, is the she-goat he carries on his shoulder.",
    "Boo": "Boötes is the herdsman driving the Great Bear around the pole; Arcturus, the bear-guard, is its brightest star.",
    "Cae": "Caelum is the sculptor's chisel, a faint southern figure added by Lacaille.",
    "Cam": "Camelopardalis is the giraffe, a large, dim constellation filling the space between the Great Bear and Cassiopeia.",
    "Cap": "Capricornus is the sea-goat, half goat and half fish, one of the oldest figures in the zodiac.",
    "Car": "Carina is the keel of the ship Argo, and Canopus, its brightest star, is the second-brightest in the night sky.",
    "Cas": "Cassiopeia is the vain queen chained to her throne, circling the pole as a bright W or M.",
    "Cen": "Centaurus is the wise centaur Chiron; Alpha Centauri, at his forefoot, is the nearest star system to the Sun.",
    "Cep": "Cepheus is the king of Ethiopia, Cassiopeia's husband and Andromeda's father, drawn as a house-shaped figure near the pole.",
    "Cet": "Cetus is the sea monster sent to devour Andromeda, sprawling below Pisces and Aries.",
    "Cha": "Chamaeleon is the chameleon, a small southern figure from the charts of the Dutch navigators.",
    "Cir": "Circinus is the drawing compass, tucked in beside Alpha Centauri.",
    "CMa": "Canis Major is Orion's great hunting dog; Sirius, in its jaw, is the brightest star in the night sky.",
    "CMi": "Canis Minor is the lesser dog, little more than the bright star Procyon, which rises just before Sirius.",
    "Cnc": "Cancer is the crab that Hera sent to pinch Heracles; the Beehive cluster glows at its heart.",
    "Col": "Columba is the dove, sent out from Noah's ark on the later star maps, flying just south of the hare.",
    "Com": "Coma Berenices is the hair of Queen Berenice, cut off as an offering and set among the stars.",
    "CrA": "Corona Australis is the southern crown, a small arc of stars at the feet of Sagittarius.",
    "CrB": "Corona Borealis is the crown Dionysus gave to Ariadne, a neat semicircle east of Boötes.",
    "Crt": "Crater is the cup of Apollo, sitting on the back of Hydra beside the crow.",
    "Cru": "Crux is the Southern Cross, the smallest constellation of all and a compass for southern navigators.",
    "Crv": "Corvus is Apollo's crow, punished for lying and set in the sky where it can never reach the cup.",
    "CVn": "Canes Venatici are the hunting dogs held on a leash by Boötes; Cor Caroli, the brighter, is named for King Charles.",
    "Cyg": "Cygnus is the swan flying down the Milky Way; Deneb marks its tail, and the Northern Cross is its outline.",
    "Del": "Delphinus is the dolphin that carried the poet Arion to safety, a small diamond of stars.",
    "Dor": "Dorado is the dolphinfish; the Large Magellanic Cloud lies within its borders.",
    "Dra": "Draco is the dragon that guarded the golden apples, winding between the two bears.",
    "Equ": "Equuleus is the little horse, the second-smallest constellation, just a head beside Pegasus.",
    "Eri": "Eridanus is the river into which Phaethon fell, winding from Orion's foot to Achernar in the far south.",
    "For": "Fornax is the chemical furnace, another of Lacaille's instruments, set in a bend of the river Eridanus.",
    "Gem": "Gemini are the twins Castor and Pollux, one mortal and one immortal, inseparable in the sky.",
    "Gru": "Grus is the crane, a long-necked southern bird beneath the southern fish.",
    "Her": "Hercules is the hero kneeling upside down with his club raised; the great globular cluster M13 sits in his torso.",
    "Hor": "Horologium is the pendulum clock, a faint Lacaille figure in the southern sky.",
    "Hya": "Hydra is the many-headed water snake Heracles slew, the longest constellation of all.",
    "Hyi": "Hydrus is the lesser water snake, coiled between the two Magellanic Clouds.",
    "Ind": "Indus is the Indian, a figure from the Dutch navigators' charts, holding arrows.",
    "Lac": "Lacerta is the lizard, a zigzag of faint stars between Cygnus and Andromeda.",
    "Leo": "Leo is the Nemean lion Heracles strangled; Regulus, the little king, sits at its heart.",
    "Lep": "Lepus is the hare crouched at Orion's feet, forever chased by his dogs.",
    "Lib": "Libra is the scales of justice, which were once the claws of the neighbouring scorpion.",
    "LMi": "Leo Minor is the little lion, a faint figure squeezed between Leo and the Great Bear.",
    "Lup": "Lupus is the wolf, held on a spear by the neighbouring centaur.",
    "Lyn": "Lynx is the lynx, named by Hevelius because you need a lynx's eyes to see it.",
    "Lyr": "Lyra is the lyre of Orpheus, whose music charmed the underworld; Vega is its brightest string.",
    "Men": "Mensa is Table Mountain, the only constellation named for a place on Earth, holding part of the Large Magellanic Cloud.",
    "Mic": "Microscopium is the microscope, one of Lacaille's instruments, south of Capricornus.",
    "Mon": "Monoceros is the unicorn, galloping through the Milky Way between Orion's two dogs.",
    "Mus": "Musca is the fly, buzzing just south of the Southern Cross.",
    "Nor": "Norma is the carpenter's square, a faint figure in the southern Milky Way.",
    "Oct": "Octans is the navigator's octant, home to the south celestial pole.",
    "Oph": "Ophiuchus is the serpent-bearer, the healer Asclepius holding a snake; the Sun passes through it each December.",
    "Ori": "Orion is the hunter with a belt of three stars; red Betelgeuse is his shoulder and blue Rigel his foot.",
    "Pav": "Pavo is the peacock, a southern bird whose brightest star is simply called Peacock.",
    "Peg": "Pegasus is the winged horse born from Medusa's blood; the Great Square is its body.",
    "Per": "Perseus is the hero who slew Medusa; Algol, the demon star, is her winking eye in his hand.",
    "Phe": "Phoenix is the bird that rises from its own ashes, a southern figure near Achernar.",
    "Pic": "Pictor is the painter's easel, a faint figure beside brilliant Canopus.",
    "PsA": "Piscis Austrinus is the southern fish, drinking the water poured by Aquarius; Fomalhaut is its mouth.",
    "Psc": "Pisces are two fish tied together by their tails, the form Aphrodite and her son took to escape a monster.",
    "Pup": "Puppis is the stern of the ship Argo, the largest of the three pieces the old ship was cut into.",
    "Pyx": "Pyxis is the mariner's compass, placed beside the ship Argo by Lacaille.",
    "Ret": "Reticulum is the reticle, the crosshair in a telescope's eyepiece, a small southern diamond.",
    "Scl": "Sculptor is the sculptor's studio, a faint region that holds the south pole of our galaxy.",
    "Sco": "Scorpius is the scorpion that stung Orion, placed opposite him in the sky; red Antares is its heart.",
    "Sct": "Scutum is the shield of the Polish king Sobieski, set in one of the brightest patches of the Milky Way.",
    "Ser": "Serpens is the snake held by Ophiuchus, the only constellation split into two parts, head and tail.",
    "Sex": "Sextans is the astronomical sextant, a faint figure Hevelius placed below Leo.",
    "Sge": "Sagitta is the arrow, a small figure in the Milky Way, said to be the arrow of Heracles or of Cupid.",
    "Sgr": "Sagittarius is the archer, a centaur drawing his bow at the scorpion; the centre of our galaxy lies in its direction.",
    "Tau": "Taurus is the bull, the form Zeus took to carry off Europa; red Aldebaran is its eye and the Pleiades ride its shoulder.",
    "Tel": "Telescopium is the telescope, a faint Lacaille figure south of Sagittarius.",
    "TrA": "Triangulum Australe is the southern triangle, three bright stars near Alpha Centauri.",
    "Tri": "Triangulum is the triangle, a small northern figure that holds the Pinwheel Galaxy.",
    "Tuc": "Tucana is the toucan, with the Small Magellanic Cloud at its feet.",
    "UMa": "Ursa Major is the Great Bear, and the Big Dipper is its hindquarters and tail; two of its stars point the way to Polaris.",
    "UMi": "Ursa Minor is the Little Bear, whose tail ends at Polaris, the star that does not move.",
    "Vel": "Vela is the sails of the ship Argo, spread across the southern Milky Way.",
    "Vir": "Virgo is the maiden of the harvest, holding an ear of wheat: the bright star Spica.",
    "Vol": "Volans is the flying fish, gliding beneath the keel of Argo.",
    "Vul": "Vulpecula is the little fox, home to the Dumbbell Nebula, in the Milky Way below Cygnus.",
}

# How many constellations get a sentence: a wide field can hold eight, and
# eight sentences is a lecture. The two whose brightest confirmed star is
# brightest are the two the eye lands on.
MAX_LINES = 2


def _star_constellations():
    from . import solver
    return {s["name"]: s["con"] for s in solver.load_catalog() if s.get("con")}


def annotate(figures, labels, catalog=None):
    """[{abbr, name, line}] for up to MAX_LINES constellations: those with a
    figure drawn and at least one confirmed star in the frame, ranked by
    that star's brightness. Unverified labels (no status) count as seen,
    since without verification nothing can say otherwise."""
    if not figures:
        return []
    con_of = catalog if catalog is not None else _star_constellations()
    best = {}
    for lab in labels or []:
        if lab.get("kind", "star") != "star" or lab.get("status") == "hidden":
            continue
        con = con_of.get(lab.get("name"))
        mag = lab.get("mag")
        if con is None or mag is None:
            continue
        if con not in best or mag < best[con]:
            best[con] = mag
    ranked = sorted(
        (fig for fig in figures if fig.get("abbr") in best and fig.get("abbr") in LORE),
        key=lambda fig: best[fig["abbr"]])
    return [{"abbr": fig["abbr"], "name": fig.get("name") or fig["abbr"],
             "line": LORE[fig["abbr"]]} for fig in ranked[:MAX_LINES]]

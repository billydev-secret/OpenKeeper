"""Channel word clouds — corpus acquisition, tokenising and rendering.

``logic`` is pure and holds the behaviour worth testing; ``corpus`` ranks
channels by recent traffic (the words themselves are always fetched live from
Discord); ``render`` turns counts into a PNG. The Discord surface is
``cogs.word_cloud_cog``.
"""

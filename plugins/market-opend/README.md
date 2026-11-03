# OpenD market plugin

`om-market-opend-plugin` exports `om_market_opend.OpenDMarketDataProvider`.
It supplies OpenD quotes, option chains, expirations, trading calendars and
earnings observations through OM's existing evidence and transport owners.

Install the official `market-opend` plugin with `om plugins install market-opend --confirm`.
Python 3.12 or newer is required. The package declares no pip dependencies;
the OpenD/Futu SDK is supplied by the OM runtime. An SDK installation alone
does not enable this capability.

The configured identity remains `market_data.provider: opend`; historical
`opend`/`futu` source evidence is unchanged. Help, schema and frozen history
remain readable without the plugin. Current market operations require it
before contacting OpenD. The plugin grants no broker account access and
does not enable, install or select any broker plugin.

# Request Tracker settings of the fixture. The database comes from the
# environment docker-compose.fixture.yml sets, so one image serves PostgreSQL
# and MySQL.

Set($rtname, 'fixture.example.com');
Set($Organization, 'fixture.example.com');
Set($WebDomain, 'localhost');
Set($WebPort, 80);
Set($Timezone, 'Asia/Bangkok');

Set($DatabaseType, $ENV{RT_DB_TYPE});
Set($DatabaseHost, $ENV{RT_DB_HOST});
Set($DatabasePort, $ENV{RT_DB_PORT});
Set($DatabaseName, 'rt');
Set($DatabaseUser, 'rt_user');
Set($DatabasePassword, 'rt_pass');
# MySQL accounts name the host they connect from; RT connects from another container.
Set($DatabaseRTHost, '%');
Set($SecretKey, 'fixture-only-secret-not-used-anywhere-else');

# Attachments over this size are moved to the Disk backend when the seed runs
# rt-externalize-attachments; smaller ones stay in the database.
Set(%ExternalStorage, Type => 'Disk', Path => '/opt/rt/var/external-storage');
Set($ExternalStorageCutoffSize, 4096);

# Nothing leaves the container.
Set($MailCommand, 'testfile');
Set($LogToSTDERR, 'warning');
Set($LogToSyslog, undef);

1;
